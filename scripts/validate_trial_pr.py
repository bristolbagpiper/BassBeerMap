#!/usr/bin/env python3
"""Classify trial PR changes and validate eligible BassBeerMap data payloads."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DATA_PATHS = frozenset({
    'coordinate-verification.json',
    'directory-release.json',
    'hermes-google-status.json',
    'location-review.json',
    'venue-registry.json',
})
RELEASE_PATHS = frozenset({
    'coordinate-verification.json',
    'directory-release.json',
    'location-review.json',
    'venue-registry.json',
})
ALERT_STATE_PATH = 'location-alert-state.json'  # downstream receipt only; never a PR auto-merge input
EXECUTION_ID_RE = re.compile(r'^[0-9a-f]{32}$')
COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_STATUS_AGE = timedelta(hours=48)


@dataclass(frozen=True)
class Change:
    status: str
    path: str
    old_mode: str = '100644'
    new_mode: str = '100644'


def changed_paths(repo_root, base_sha, head_sha):
    """Return exact file/status pairs from the candidate repository history."""
    if not base_sha or set(base_sha) == {'0'}:
        raise ValueError('Push has no usable prior commit; this event is not data-merge eligible')
    result = subprocess.run(
        ['git', 'diff', '--no-renames', '--raw', '-z', base_sha, head_sha],
        cwd=repo_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    fields = result.stdout.decode('utf-8').split('\0')
    if fields and fields[-1] == '':
        fields.pop()
    changes = []
    i = 0
    while i < len(fields):
        header = fields[i].split()
        if len(header) != 5 or not header[0].startswith(':') or i + 1 >= len(fields):
            raise ValueError('Git returned an incomplete changed-path record')
        changes.append(Change(header[4], fields[i + 1], header[0][1:], header[1]))
        i += 2
    return changes


def _data_changes(changes):
    """Validate file operations for exempt data paths and return their subset."""
    if not changes:
        return [], 'no changed files'
    selected = []
    for change in changes:
        if (not change.path or '\\' in change.path or change.path.startswith('/')
                or '..' in Path(change.path).parts):
            if change.path in DATA_PATHS:
                raise ValueError(f'unsafe exempt-data path: {change.path!r}')
            continue
        if change.path not in DATA_PATHS:
            continue
        if change.status != 'M':
            raise ValueError(f'{change.status} is forbidden for exempt-data path {change.path}')
        if change.old_mode != '100644' or change.new_mode != '100644':
            raise ValueError(f'non-regular file mode is forbidden for exempt-data path {change.path}')
        selected.append(change)
    return selected, 'no exempt data paths'


def _strict_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key {key!r} in {Path(path).name}')
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f'non-finite JSON number {value} in {Path(path).name}')

    with Path(path).open(encoding='utf-8') as source:
        return json.load(source, object_pairs_hook=unique_pairs, parse_constant=reject_constant)


def _read_json(path):
    value = _strict_json(path)
    if not isinstance(value, dict):
        raise ValueError(f'{Path(path).name} must contain a JSON object')
    return value


def _utc(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f'{field} is missing')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError(f'{field} is not an ISO-8601 timestamp') from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f'{field} must use UTC')
    return parsed.astimezone(timezone.utc)


def validate_hermes_status(root, *, now=None):
    """Validate the native receipt shape and bind it to the checked release."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    config = _read_json(root / 'google-review-provider.json')
    if config != {'provider': 'hermes-browser'}:
        raise ValueError('Google provider configuration must be exactly hermes-browser')
    status = _read_json(root / 'hermes-google-status.json')
    if type(status.get('schema_version')) is not int or status['schema_version'] != 1:
        raise ValueError('Hermes status schema version is unsupported')
    if status.get('provider') != 'hermes-browser' or status.get('configured') is not True:
        raise ValueError('Hermes status provider/configuration does not match production')
    if status.get('execution_state') != 'complete' or status.get('hard_failure') is not False:
        raise ValueError('Hermes source execution is not complete and successful')
    if type(status.get('run_attempt')) is not int or status['run_attempt'] != 1:
        raise ValueError('Hermes run attempt is invalid')
    if not isinstance(status.get('run_id'), str) or not EXECUTION_ID_RE.fullmatch(status['run_id']):
        raise ValueError('Hermes run ID is invalid')
    if not isinstance(status.get('source_commit'), str) or not COMMIT_RE.fullmatch(status['source_commit']):
        raise ValueError('Hermes source commit is invalid')
    for field in ('directory_binding_signature', 'source_release_id', 'validated_release_id', 'source_status_sha256'):
        if not isinstance(status.get(field), str) or not SHA256_RE.fullmatch(status[field]):
            raise ValueError(f'Hermes {field} is invalid')
    checked = _utc(status.get('checked_at'), 'checked_at')
    finished = _utc(status.get('source_execution_finished_at'), 'source_execution_finished_at')
    if not timedelta(0) <= now - finished <= MAX_STATUS_AGE:
        raise ValueError('Hermes source status is stale or from the future')
    if not timedelta(0) <= now - checked <= MAX_STATUS_AGE:
        raise ValueError('Hermes checked_at is stale or from the future')
    if checked > finished + timedelta(minutes=5):
        raise ValueError('Hermes checked_at is later than source completion')
    batch = status.get('batch')
    required_batch = {'selected', 'attempted', 'checked', 'ambiguous', 'unavailable'}
    if (not isinstance(batch, dict) or set(batch) != required_batch
            or any(type(batch.get(key)) is not int or batch[key] < 0 for key in required_batch)
            or batch['attempted'] > batch['selected']
            or batch['checked'] + batch['unavailable'] != batch['attempted']
            or batch['ambiguous'] > batch['checked']):
        raise ValueError('Hermes batch is malformed or inconsistent')
    counts = status.get('counts')
    count_keys = ('total', 'current', 'unchecked_or_stale')
    if (not isinstance(counts, dict)
            or any(type(counts.get(key)) is not int or counts[key] < 0 for key in count_keys)
            or counts['total'] != counts['current'] + counts['unchecked_or_stale']):
        raise ValueError('Hermes coverage counts are malformed or inconsistent')

    # Reuse the application validator to check findings and directory binding.
    from directory_release import read_json
    from location_review import (GOOGLE_COVERAGE_ISSUE, GOOGLE_STALE_STATUS_ISSUE,
                                 load_google_status, with_google)
    loaded = load_google_status(root / 'google-places-status.json')
    review = read_json(root / 'location-review.json')
    bundle = read_json(root / 'directory-release.json')
    report = with_google(review, loaded, bundle, now=now)
    if GOOGLE_STALE_STATUS_ISSUE in report.get('google_issues', []):
        raise ValueError('Application review reports a stale Hermes receipt')
    if GOOGLE_COVERAGE_ISSUE in report.get('google_issues', []):
        raise ValueError('Application review reports incomplete Hermes coverage')
    if report.get('google_hard_failure'):
        raise ValueError('Application review reports a Hermes source failure')


def validate_data_payloads(base_root, candidate_root, changes, *, now=None):
    base_root, candidate_root = Path(base_root), Path(candidate_root)
    data_changes, reason = _data_changes(changes)
    if not data_changes:
        return False, reason
    changed = {change.path for change in data_changes}
    for name in changed:
        value = _strict_json(candidate_root / name)
        if not isinstance(value, dict):
            raise ValueError(f'{name} must contain a JSON object')
    sys.path.insert(0, str(base_root))
    if changed & RELEASE_PATHS:
        from directory_release import validate_release
        errors = validate_release(candidate_root, previous_root=base_root)
        if errors:
            raise ValueError('Canonical release validation failed: ' + '; '.join(errors[:8]))
    if 'hermes-google-status.json' in changed:
        validate_hermes_status(candidate_root, now=now)
    data_only = len(data_changes) == len(changes)
    if data_only:
        return True, 'exact allowlisted data paths only; all changed payloads passed strict validation'
    return False, 'mixed/code changes; allowlisted data subset passed validation but is not auto-merge eligible'


def _set_output(name, value):
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as handle:
            handle.write(f'{name}={value}\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-root', required=True, type=Path)
    parser.add_argument('--candidate-root', required=True, type=Path)
    parser.add_argument('--base-sha', required=True)
    parser.add_argument('--head-sha', required=True)
    parser.add_argument('--now', help='UTC ISO timestamp override for deterministic fixture checks')
    args = parser.parse_args(argv)
    now = _utc(args.now, '--now') if args.now else datetime.now(timezone.utc)
    try:
        changes = changed_paths(args.candidate_root, args.base_sha, args.head_sha)
        eligible, reason = validate_data_payloads(args.base_root, args.candidate_root, changes, now=now)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        _set_output('merge_eligible', 'false')
        print(f'Trial data policy failed: {error}', file=sys.stderr)
        return 1
    _set_output('merge_eligible', str(eligible).lower())
    print(f'merge_eligible={str(eligible).lower()}: {reason}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
