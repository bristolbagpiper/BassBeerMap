"""Prepare a sanitized, review-only Google status handoff from a local run."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audit_google_places import COVERAGE_ISSUE
from hermes_google_runner import validate_inputs
from hermes_google_review import build_status, parse_utc
from location_review import with_google


MAX_EXECUTION_AGE = timedelta(hours=48)
MAX_CHECKED_AFTER_FINISH = timedelta(minutes=5)
EXECUTION_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
COMMIT_PATTERN = re.compile(r'^[0-9a-f]{40}$')
RELEASE_PATTERN = re.compile(r'^[0-9a-f]{64}$')
STATUS_FIELDS = (
    'schema_version', 'checked_at', 'configured', 'directory_binding_signature',
    'issues', 'findings', 'counts', 'hard_failure',
)
BATCH_FIELDS = ('selected', 'attempted', 'checked', 'ambiguous', 'unavailable')
FINDING_FIELDS = (
    'venue_id', 'pub_name', 'place_name', 'postcode', 'status', 'reason', 'binding_signature',
)
FINDING_REASONS = frozenset((
    'Google named-place result differs in business identity or postcode; verify the matched venue and address',
    'Google named-place marker differs by more than 50 metres; existing pin retained for review',
    'Google indicates a possible closure; trading status needs review',
    'Google results are incomplete or ambiguous; no unique place accepted',
))
SAFE_STATUS_ISSUES = frozenset((
    COVERAGE_ISSUE,
    'Google Maps could not be read for one or more venues in this batch',
))
BLOCKED_ISSUE = 'Hermes Google verification stopped before completing; this run may have incomplete coverage'
FAILED_ISSUE = 'Hermes Google verification failed before a verified status was produced'


def _read_json_snapshot(path, *, required=True):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise ValueError(f'Required input is missing: {path.name}') from None
        return None
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Invalid JSON input: {path.name}') from error
    return raw, value, hashlib.sha256(raw).hexdigest()


def _copy_snapshot(root, snapshots):
    for relative, (raw, _, _) in snapshots.items():
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def _input_tree(source, *, include_audit=True):
    """Read a stable input snapshot into a temporary build_status root."""
    source = Path(source)
    snapshots = {
        'directory-release.json': _read_json_snapshot(source / 'directory-release.json'),
        'venue-registry.json': _read_json_snapshot(source / 'venue-registry.json'),
    }
    if include_audit:
        audit = _read_json_snapshot(source / 'audit' / 'full-location-audit.json', required=False)
        if audit:
            snapshots['audit/full-location-audit.json'] = audit
    root = tempfile.TemporaryDirectory(prefix='hermes-google-publish-')
    _copy_snapshot(root.name, snapshots)
    return root, snapshots


def _validate_commit(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not COMMIT_PATTERN.fullmatch(value):
        raise ValueError('Invalid source commit')
    return value


def _validate_release(value):
    if not isinstance(value, str) or not RELEASE_PATTERN.fullmatch(value):
        raise ValueError('Invalid release identifier')
    return value


def _validate_batch(value):
    if not isinstance(value, dict) or set(value) != set(BATCH_FIELDS):
        raise ValueError('Invalid run batch')
    batch = {}
    for field in BATCH_FIELDS:
        count = value[field]
        if type(count) is not int or count < 0:
            raise ValueError('Invalid run batch count')
        batch[field] = count
    if (batch['attempted'] > batch['selected']
            or batch['checked'] + batch['unavailable'] != batch['attempted']
            or batch['ambiguous'] > batch['checked']):
        raise ValueError('Inconsistent run batch counts')
    return batch


def _run_attempt_count(receipts, started, finished):
    attempts = set()
    for item in receipts['observations']:
        attempted = parse_utc(item['attempted_at'])
        if started <= attempted <= finished:
            attempts.add((item['venue_id'], attempted))
    return len(attempts)


def _validate_safe_status(status):
    if not isinstance(status, dict):
        raise ValueError('Invalid source status')
    if any(issue not in SAFE_STATUS_ISSUES for issue in status.get('issues', [])):
        raise ValueError('Source status contains an unsupported issue')
    for finding in status.get('findings', []):
        if any(field not in finding for field in FINDING_FIELDS):
            raise ValueError('Source status contains an incomplete finding')
        if finding.get('status') != 'review_needed':
            raise ValueError('Source status contains an unsupported finding status')
        remaining = finding['reason']
        matched = 0
        for reason in sorted(FINDING_REASONS, key=len, reverse=True):
            if reason in remaining:
                remaining = remaining.replace(reason, '')
                matched += 1
        if not matched or remaining.replace(';', '').strip() or len(finding['reason']) > 600:
            raise ValueError('Source status contains an unsupported finding reason')


def _validate_status_matches(status, rebuilt, execution, batch):
    for field in STATUS_FIELDS:
        if status.get(field) != rebuilt.get(field):
            raise ValueError('Saved status does not match the validated observation journal')
    if status.get('run_id') != execution['execution_id']:
        raise ValueError('Saved status belongs to a different execution')
    if status.get('source_execution_finished_at') != execution['finished_at']:
        raise ValueError('Saved status finish time does not match the execution receipt')
    if status.get('batch') != batch:
        raise ValueError('Saved status batch does not match the execution receipt')
    _validate_safe_status(status)


def _project_findings(findings):
    projected = []
    for finding in findings:
        # These are the only reportable finding fields. In particular, Google
        # result/source URLs and closure-only timestamp/point/status fields stay local.
        projected.append({field: finding[field] for field in FINDING_FIELDS})
    return projected


def _safe_issues(issues, terminal_state):
    result = {issue for issue in issues if issue in SAFE_STATUS_ISSUES}
    if terminal_state == 'blocked':
        result.add(BLOCKED_ISSUE)
    elif terminal_state == 'failed':
        result.add(FAILED_ISSUE)
    return sorted(result)


def _execution_envelope(status, *, execution, source_commit, source_release,
                        validated_release, status_hash, execution_state, batch):
    counts = dict(status['counts'])
    findings = _project_findings(status['findings'])
    if execution_state == 'blocked':
        total = counts.get('total', 0)
        counts.update(current=0, unchecked_or_stale=total,
                      attempted_venue_count=0, review_count=0)
        findings = []
    return {
        'schema_version': status['schema_version'],
        'checked_at': status['checked_at'],
        'configured': status['configured'],
        'directory_binding_signature': status['directory_binding_signature'],
        'issues': _safe_issues(status['issues'], execution_state),
        'findings': findings,
        'counts': counts,
        'hard_failure': execution_state == 'failed' or status['hard_failure'],
        'provider': 'hermes-browser',
        'run_id': execution['execution_id'],
        'run_attempt': 1,
        'batch': batch,
        'source_execution_finished_at': execution['finished_at'],
        'source_commit': source_commit,
        'source_release_id': source_release,
        'validated_release_id': validated_release,
        'source_status_sha256': status_hash,
        'execution_state': execution_state,
    }


def _read_execution(state_dir):
    last_run_snapshot = _read_json_snapshot(Path(state_dir) / 'last-run.json')
    execution = last_run_snapshot[1]
    if not isinstance(execution, dict) or type(execution.get('schema_version')) is not int or execution['schema_version'] != 1:
        raise ValueError('Invalid execution receipt')
    if not EXECUTION_ID_PATTERN.fullmatch(str(execution.get('execution_id', ''))):
        raise ValueError('Invalid execution identifier')
    if execution.get('state') not in ('complete', 'blocked', 'failed'):
        raise ValueError('Execution is not in a supported terminal state')
    started = parse_utc(execution.get('started_at'))
    finished = parse_utc(execution.get('finished_at'))
    if finished < started:
        raise ValueError('Execution finished before it started')
    return last_run_snapshot, execution, started, finished


def _git_object_json(repository, commit, relative, *, required=False):
    """Read one exact commit object without changing the repository checkout."""
    repository = Path(repository).resolve()
    object_name = f'{commit}:{relative}'
    exists = subprocess.run(['git', 'cat-file', '-e', object_name], cwd=repository,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if exists.returncode != 0:
        if required:
            raise ValueError(f'Requested commit is unavailable locally: {relative}')
        return None
    result = subprocess.run(['git', 'show', object_name], cwd=repository,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0 or len(result.stdout) > 24_000_000:
        raise ValueError(f'Could not read bounded Git object: {relative}')
    try:
        return json.loads(result.stdout.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Invalid Git JSON object: {relative}') from error


def _stage_commit_data(repository, commit, destination):
    for relative in ('directory-release.json', 'venue-registry.json'):
        value = _git_object_json(repository, commit, relative, required=True)
        target = Path(destination) / relative
        target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8', newline='\n')


def _verify_source_commit_objects(repository, commit, source_bundle, source_registry):
    if repository is None:
        return
    bundle_object = _git_object_json(repository, commit, 'directory-release.json')
    registry_object = _git_object_json(repository, commit, 'venue-registry.json')
    if bundle_object is not None and bundle_object != source_bundle:
        raise ValueError('Saved source release differs from its recorded Git commit')
    if registry_object is not None and registry_object != source_registry:
        raise ValueError('Saved source registry differs from its recorded Git commit')


def _check_no_active_run(state_dir):
    for name in ('runner.lock', 'observations.json.lock'):
        if (Path(state_dir) / name).exists():
            raise ValueError('An active Hermes Google execution is still writing local state')


def prepare_candidate(state_dir, current_data_root, current_commit, now=None, *, repository=None):
    """Build a review-only status handoff, without changing the source state."""
    state_dir = Path(state_dir).resolve()
    current_data_root = Path(current_data_root).resolve()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError('Current time must be UTC')
    current_commit = _validate_commit(current_commit)
    if repository is not None:
        repository = Path(repository).resolve()
    _check_no_active_run(state_dir)
    last_run_snapshot, execution, started, finished = _read_execution(state_dir)
    if not timedelta(0) <= now - finished <= MAX_EXECUTION_AGE:
        raise ValueError('Execution is stale or has a future finish time')

    current_temp, current_snapshots = _input_tree(current_data_root)
    try:
        current_bundle, current_registry = current_snapshots['directory-release.json'][1], current_snapshots['venue-registry.json'][1]
        validate_inputs(current_bundle, current_registry)
        _verify_source_commit_objects(repository, current_commit, current_bundle, current_registry)
        validated_release = _validate_release(current_bundle.get('release_id'))

        execution_state = execution['state']
        source_commit = None
        source_release = None
        status_hash = None
        batch = {field: 0 for field in BATCH_FIELDS}
        if execution_state == 'failed':
            # Failed runs are diagnostic-only: never attach retained status or
            # old observations as if they were fresh results from this execution.
            source_root = state_dir / 'data'
            try:
                source_temp, source_snapshots = _input_tree(source_root, include_audit=False)
            except (OSError, ValueError):
                source_temp, source_snapshots = None, {}
            if source_temp:
                try:
                    source_bundle = source_snapshots['directory-release.json'][1]
                    source_registry = source_snapshots['venue-registry.json'][1]
                    validate_inputs(source_bundle, source_registry)
                    possible_commit = execution.get('source_commit')
                    possible_release = execution.get('release_id')
                    if (isinstance(possible_commit, str) and COMMIT_PATTERN.fullmatch(possible_commit)
                            and isinstance(possible_release, str)
                            and possible_release == source_bundle.get('release_id')):
                        _verify_source_commit_objects(repository, possible_commit, source_bundle, source_registry)
                        source_commit = possible_commit
                        source_release = _validate_release(possible_release)
                finally:
                    source_temp.cleanup()
            failure_status = build_status(current_temp.name,
                                          {'schema_version': 1, 'observations': []},
                                          now=finished, batch=batch)
            envelope = _execution_envelope(
                failure_status, execution=execution, source_commit=source_commit,
                source_release=source_release, validated_release=validated_release,
                status_hash=None, execution_state='failed', batch=batch)
        else:
            batch = _validate_batch(execution.get('batch'))
            if execution_state == 'complete' and batch['attempted'] != batch['selected']:
                raise ValueError('Completed execution did not attempt the full selected batch')
            if execution.get('observations_written') != batch['attempted']:
                raise ValueError('Execution observation count does not match its batch')

            receipts_snapshot = _read_json_snapshot(state_dir / 'observations.json')
            receipts = receipts_snapshot[1]
            expected_observations_hash = execution.get('observations_sha256')
            if (expected_observations_hash is not None
                    and expected_observations_hash != receipts_snapshot[2]):
                raise ValueError('Observation journal hash does not match the execution receipt')
            if _run_attempt_count(receipts, started, finished) != batch['attempted']:
                raise ValueError('Observation journal attempt count does not match the execution receipt')

            status_snapshot = _read_json_snapshot(state_dir / 'hermes-google-status.json')
            status_raw, saved_status, status_hash = status_snapshot
            if not isinstance(saved_status, dict):
                raise ValueError('Invalid saved status')
            if hashlib.sha256(status_raw).hexdigest() != execution.get('status_sha256'):
                raise ValueError('Saved status hash does not match the execution receipt')

            source_commit = _validate_commit(execution.get('source_commit'))
            source_root = state_dir / 'data'
            source_temp, source_snapshots = _input_tree(source_root, include_audit=False)
            try:
                source_bundle = source_snapshots['directory-release.json'][1]
                source_registry = source_snapshots['venue-registry.json'][1]
                validate_inputs(source_bundle, source_registry)
                source_release = _validate_release(execution.get('release_id'))
                if source_bundle.get('release_id') != source_release:
                    raise ValueError('Execution release does not match its source snapshot')
                _verify_source_commit_objects(repository, source_commit, source_bundle, source_registry)

                checked_at = parse_utc(saved_status.get('checked_at'))
                if checked_at < started or checked_at > finished + MAX_CHECKED_AFTER_FINISH:
                    raise ValueError('Saved status time is outside the execution window')
                if saved_status.get('batch') != batch:
                    raise ValueError('Saved status batch does not match the execution receipt')
                source_rebuilt = build_status(source_temp.name, receipts, now=checked_at, batch=batch)
                _validate_status_matches(saved_status, source_rebuilt, execution, batch)
            finally:
                source_temp.cleanup()

            current_status = build_status(current_temp.name, receipts, now=checked_at, batch=batch)
            _validate_safe_status(current_status)
            envelope = _execution_envelope(
                current_status, execution=execution, source_commit=source_commit,
                source_release=source_release, validated_release=validated_release,
                status_hash=status_hash, execution_state=execution_state, batch=batch)

        with_google({'records': {}}, envelope, current_bundle,
                    now=parse_utc(envelope['checked_at']))

        # Detect a concurrent runner or repository update before returning a
        # handoff assembled from a mixed set of inputs.
        if _read_json_snapshot(state_dir / 'last-run.json')[0] != last_run_snapshot[0]:
            raise ValueError('Execution receipt changed while preparing the handoff')
        if execution_state != 'failed':
            if _read_json_snapshot(state_dir / 'observations.json')[0] != receipts_snapshot[0]:
                raise ValueError('Observation journal changed while preparing the handoff')
            if _read_json_snapshot(state_dir / 'hermes-google-status.json')[0] != status_snapshot[0]:
                raise ValueError('Saved status changed while preparing the handoff')
        for relative, snapshot in current_snapshots.items():
            if _read_json_snapshot(current_data_root / relative, required=relative != 'audit/full-location-audit.json') != snapshot:
                raise ValueError('Current GitHub data changed while preparing the handoff')
        _check_no_active_run(state_dir)
        return envelope
    finally:
        current_temp.cleanup()


def write_candidate(path, envelope, *, state_dir, current_data_root, repository=None):
    """Write a new handoff file without overwriting files or runtime state."""
    path = Path(path).resolve()
    forbidden_roots = (Path(state_dir).resolve(), Path(current_data_root).resolve())
    if repository is not None:
        forbidden_roots += (Path(repository).resolve(),)
    if any(path == root or root in path.parents for root in forbidden_roots):
        raise ValueError('Handoff output must be outside runtime and source data directories')
    if not path.parent.is_dir():
        raise ValueError('Handoff output directory must already exist')
    payload = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True,
                        help='Hermes local state directory containing last-run.json and observations.json')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--current-data-root', type=Path,
                        help='Fetched current repository checkout containing the release and registry')
    source.add_argument('--repository', type=Path,
                        help='Fetched Git repository; read bundle and registry from --current-commit via git show')
    parser.add_argument('--current-commit', required=True,
                        help='Explicit 40-character GitHub commit for the current data')
    parser.add_argument('--output', type=Path,
                        help='Create a new sanitized JSON handoff file; defaults to stdout')
    args = parser.parse_args(argv)
    if args.repository:
        current_commit = _validate_commit(args.current_commit)
        with tempfile.TemporaryDirectory(prefix='hermes-google-current-') as staged:
            _stage_commit_data(args.repository, current_commit, staged)
            envelope = prepare_candidate(args.state_dir, staged, current_commit,
                                         repository=args.repository)
            if args.output:
                write_candidate(args.output, envelope, state_dir=args.state_dir,
                                current_data_root=staged, repository=args.repository)
            else:
                print(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        envelope = prepare_candidate(args.state_dir, args.current_data_root, args.current_commit)
        if args.output:
            write_candidate(args.output, envelope, state_dir=args.state_dir,
                            current_data_root=args.current_data_root)
        else:
            print(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
