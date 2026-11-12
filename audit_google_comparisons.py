"""Route the daily Google comparison job to the configured local provider."""
import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import audit_google_places
from build_place_review_queue import digest, signatures
from directory_release import read_json, write_json
from location_review import with_google

MISSING_ISSUE = 'Hermes Google browser results are missing; no live comparison has passed'
INVALID_ISSUE = 'Hermes Google browser status is invalid; inspect the local receipt consumer'
CONFIG_PATH = 'google-review-provider.json'


def _now(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError('Current time must be UTC')
    return value


def directory_binding(bundle):
    return digest(sorted((row['venue_id'], signatures(row, bundle['locations'][row['venue_id']])['binding_signature'])
                         for row in bundle['rows']))


def _base_status(bundle, now, issues, hard_failure=False):
    total = len(bundle['rows'])
    return dict(schema_version=1, checked_at=now.isoformat(), source_execution_finished_at=now.isoformat(),
                execution_state='failed', source_commit=None, source_release_id=None,
                validated_release_id=bundle['release_id'], source_status_sha256=None,
                run_id=os.environ.get('AUDIT_RUN_ID', 'diagnostic'), batch={},
                configured=True, provider='hermes-browser',
                directory_binding_signature=directory_binding(bundle), issues=list(issues), findings=[],
                counts=dict(total=total, current=0, unchecked_or_stale=total,
                            checked_this_run=0, attempted_venue_count=0, review_count=0),
                hard_failure=hard_failure)


def _valid_source_status(value, bundle, now):
    if not isinstance(value, dict):
        raise ValueError('Status must be an object')
    # with_google is the owning schema validator and performs exact current
    # venue/signature checks on every finding.
    review = with_google(dict(records={}), value, bundle, now=now)
    if value.get('provider') != 'hermes-browser':
        raise ValueError('Unexpected provider')
    for key, pattern in (('source_commit', r'[0-9a-f]{40}'),
                         ('source_release_id', r'[0-9a-f]{64}'),
                         ('source_status_sha256', r'[0-9a-f]{64}')):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not re.fullmatch(pattern, item)):
            raise ValueError('Malformed source identity')
    if value.get('execution_state') == 'complete' and any(value.get(key) is None for key in
            ('source_commit', 'source_release_id', 'source_status_sha256')):
        raise ValueError('Complete source receipt lacks source identity')
    if value.get('execution_state') not in ('complete', 'blocked', 'failed'):
        raise ValueError('Invalid source execution state')
    if not isinstance(value.get('validated_release_id'), str) or not value['validated_release_id']:
        raise ValueError('Missing validated release')
    if not isinstance(value.get('batch'), dict):
        raise ValueError('Malformed source batch')
    finished = value.get('source_execution_finished_at')
    if not isinstance(finished, str):
        raise ValueError('Missing execution completion time')
    try:
        finished_at = datetime.fromisoformat(finished.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Malformed execution completion time') from exc
    if finished_at.tzinfo is None or finished_at > now:
        raise ValueError('Invalid execution completion time')
    checked_at = value.get('checked_at')
    if not isinstance(checked_at, str):
        raise ValueError('Missing source checked_at')
    try:
        parsed = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Malformed source checked_at') from exc
    if parsed.tzinfo is None or parsed > now:
        raise ValueError('Invalid source checked_at')
    counts = value.get('counts')
    if not isinstance(counts, dict):
        raise ValueError('Missing counts')
    required = ('total', 'current', 'unchecked_or_stale', 'checked_this_run', 'review_count')
    if any(type(counts.get(key)) is not int or counts[key] < 0 for key in required):
        raise ValueError('Malformed counts')
    if counts['current'] + counts['unchecked_or_stale'] != counts['total']:
        raise ValueError('Inconsistent coverage counts')
    if type(value.get('hard_failure', False)) is not bool:
        raise ValueError('Malformed hard_failure')
    return review


def _hermes_status(root, bundle, now):
    source_path = Path(root) / 'hermes-google-status.json'
    if not source_path.exists():
        status = _base_status(bundle, now, [MISSING_ISSUE, audit_google_places.COVERAGE_ISSUE])
        return status, 0
    try:
        status = read_json(source_path)
        review = _valid_source_status(status, bundle, now)
        accepted = {(finding['venue_id'], finding['binding_signature']) for finding in review['google_findings']}
        status['findings'] = [finding for finding in status['findings']
                              if (finding['venue_id'], finding['binding_signature']) in accepted]
        status['issues'] = review['google_issues']
        status['counts'] = dict(review['google_counts'], review_count=len(status['findings']))
        return status, 0
    except Exception:
        status = _base_status(bundle, now, [INVALID_ISSUE, audit_google_places.COVERAGE_ISSUE], hard_failure=True)
        return status, 1


def route(root='.', state='google-places-state.json', status='google-places-status.json', limit=45,
          enabled=False, api_key='', now=None, lookup=None):
    root, state, status = Path(root), Path(state), Path(status)
    now = _now(now)
    if not 0 <= limit <= audit_google_places.MAX_REQUESTS_PER_RUN:
        raise ValueError('Google check limit must be between 0 and 45')
    config_path = root / CONFIG_PATH
    provider = 'places-api'
    if config_path.exists():
        config = read_json(config_path)
        if not isinstance(config, dict) or set(config) != {'provider'} or config['provider'] not in ('places-api', 'hermes-browser', 'hermes-local'):
            raise ValueError('Invalid Google review provider configuration')
        provider = config['provider']
    if provider == 'hermes-local':
        # Hermes sends its private report during the local execution. Do not
        # copy a prior browser/API snapshot into GitHub status or artifacts.
        return dict(provider=provider, locally_handled=True), 0
    if provider == 'hermes-browser':
        bundle = read_json(root / 'directory-release.json')
        result, code = _hermes_status(root, bundle, now)
    else:
        result = audit_google_places.audit(root, state, limit, enabled, api_key,
                                           lookup=lookup or audit_google_places.search_places, now=now)
        code = int(result['hard_failure'])
    result['provider'] = provider
    if provider == 'hermes-browser':
        result['validation_workflow_run_id'] = os.environ.get('AUDIT_RUN_ID', '')
        result['validation_workflow_run_url'] = os.environ.get('AUDIT_RUN_URL', '')
    else:
        result['run_id'] = os.environ.get('AUDIT_RUN_ID', '')
        result['run_attempt'] = int(os.environ.get('AUDIT_RUN_ATTEMPT', '1'))
        result['run_url'] = os.environ.get('AUDIT_RUN_URL', '')
    write_json(status, result)
    return result, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--state', type=Path, default=Path('google-places-state.json'))
    parser.add_argument('--status', type=Path, default=Path('google-places-status.json'))
    parser.add_argument('--limit', type=int, default=45)
    args = parser.parse_args(argv)
    enabled = os.environ.get('GOOGLE_PLACES_ENABLED', '').lower() == 'true'
    api_key = os.environ.get('GOOGLE_PLACES_API_KEY', '')
    try:
        result, code = route(args.root, args.state, args.status, args.limit, enabled, api_key)
    except Exception:
        now = datetime.now(timezone.utc)
        try:
            bundle = read_json(args.root / 'directory-release.json')
            result = _base_status(bundle, now, ['Google comparison failed before completing; inspect state and configuration'], True)
        except Exception:
            result = dict(schema_version=1, checked_at=now.isoformat(), configured=False,
                          issues=['Google comparison failed before completing; inspect state and configuration'],
                          counts={}, findings=[], hard_failure=True)
        result.update(provider='unknown', run_id=os.environ.get('AUDIT_RUN_ID', ''),
                      run_attempt=int(os.environ.get('AUDIT_RUN_ATTEMPT', '1')),
                      run_url=os.environ.get('AUDIT_RUN_URL', ''))
        write_json(args.status, result)
        code = 1
    print(json.dumps(result, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
