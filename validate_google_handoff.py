#!/usr/bin/env python3
"""Check that the required native Hermes Google handoff is current and successful."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


EXPECTED_PROVIDER = 'hermes-browser'
MAX_AGE = timedelta(hours=48)
EXECUTION_ID = re.compile(r'^[0-9a-f]{32}$')
COMMIT_ID = re.compile(r'^[0-9a-f]{40}$')
RELEASE_ID = re.compile(r'^[0-9a-f]{64}$')
BATCH_FIELDS = {'selected', 'attempted', 'checked', 'ambiguous', 'unavailable'}
COUNT_FIELDS = {'total', 'current', 'unchecked_or_stale'}


def _read_json(path: Path):
    try:
        with path.open(encoding='utf-8') as source:
            return json.load(source)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _utc_datetime(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def check_handoff(root='.', *, now=None):
    """Return a concise list of contract failures; empty means accepted."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    failures = []

    # Keep the production provider requirement independent of the mutable
    # provider configuration: changing that file cannot disable this gate.
    configured = _read_json(root / 'google-review-provider.json')
    if not isinstance(configured, dict) or configured != {'provider': EXPECTED_PROVIDER}:
        actual = configured.get('provider') if isinstance(configured, dict) else 'missing or invalid'
        failures.append(f'expected exact configured provider {EXPECTED_PROVIDER}; found {actual}')

    status = _read_json(root / 'hermes-google-status.json')
    if not isinstance(status, dict):
        failures.append('required Hermes Google status is missing or invalid')
        return failures

    if status.get('provider') != EXPECTED_PROVIDER:
        failures.append(f'expected status provider {EXPECTED_PROVIDER}; found {status.get("provider", "missing")}')
    if type(status.get('schema_version')) is not int or status['schema_version'] != 1:
        failures.append('Hermes status schema version is missing or unsupported')
    if status.get('configured') is not True:
        failures.append('Hermes source status is not configured')
    if not isinstance(status.get('run_id'), str) or not EXECUTION_ID.fullmatch(status['run_id']):
        failures.append('Hermes source run ID is missing or invalid')
    if type(status.get('run_attempt')) is not int or status['run_attempt'] != 1:
        failures.append('Hermes source run attempt is missing or invalid')
    if not isinstance(status.get('directory_binding_signature'), str) or not RELEASE_ID.fullmatch(status['directory_binding_signature']):
        failures.append('Hermes directory binding signature is missing or invalid')
    if not isinstance(status.get('source_commit'), str) or not COMMIT_ID.fullmatch(status['source_commit']):
        failures.append('Hermes source commit is missing or invalid')
    for name in ('source_release_id', 'validated_release_id', 'source_status_sha256'):
        if not isinstance(status.get(name), str) or not RELEASE_ID.fullmatch(status[name]):
            failures.append(f'Hermes {name.replace("_", " ")} is missing or invalid')
    if not isinstance(status.get('issues'), list) or not all(isinstance(item, str) for item in status['issues']):
        failures.append('Hermes status issues are malformed')
    if not isinstance(status.get('findings'), list) or not all(isinstance(item, dict) for item in status['findings']):
        failures.append('Hermes status findings are malformed')
    counts = status.get('counts')
    if (not isinstance(counts, dict)
            or any(type(counts.get(name)) is not int or counts[name] < 0 for name in COUNT_FIELDS)):
        failures.append('Hermes source coverage counts are missing or invalid')
    elif counts['total'] != counts['current'] + counts['unchecked_or_stale']:
        failures.append('Hermes source coverage counts are inconsistent')
    batch = status.get('batch')
    if (not isinstance(batch, dict) or set(batch) != BATCH_FIELDS
            or any(type(batch.get(name)) is not int or batch[name] < 0 for name in BATCH_FIELDS)):
        failures.append('Hermes execution batch is missing or invalid')
    elif (batch['attempted'] > batch['selected']
            or batch['checked'] + batch['unavailable'] != batch['attempted']
            or batch['ambiguous'] > batch['checked']):
        failures.append('Hermes execution batch counts are inconsistent')
    if status.get('execution_state') != 'complete':
        failures.append(f'latest Hermes source execution is {status.get("execution_state", "missing")}, not complete')
    if status.get('hard_failure') is not False:
        failures.append('latest Hermes source execution reports a hard failure')

    finished = _utc_datetime(status.get('source_execution_finished_at'))
    checked = _utc_datetime(status.get('checked_at'))
    if finished is None:
        failures.append('Hermes source completion time is missing or invalid')
    else:
        age = now - finished
        if age < timedelta(0):
            failures.append('Hermes source completion time is in the future')
        elif age > MAX_AGE:
            failures.append('Hermes source status is older than 48 hours')
    if checked is None:
        failures.append('Hermes status check time is missing or invalid')
    else:
        checked_age = now - checked
        if checked_age < timedelta(0):
            failures.append('Hermes status check time is in the future')
        elif checked_age > MAX_AGE:
            failures.append('Hermes status check is older than 48 hours')
        if finished is not None and checked > finished + timedelta(minutes=5):
            failures.append('Hermes status check time is later than its source completion')

    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.', help='repository root containing the handoff status files')
    args = parser.parse_args(argv)
    failures = check_handoff(args.root)
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as summary:
            summary.write('## Native Hermes Google handoff acceptance\n\n')
            if failures:
                summary.write('**Failed**\n\n')
                summary.writelines(f'- {failure}\n' for failure in failures)
            else:
                summary.write('**Passed:** configured provider and latest source receipt are current and successful.\n')
    if failures:
        print('Native Hermes Google handoff acceptance failed:', file=sys.stderr)
        for failure in failures:
            print(f'- {failure}', file=sys.stderr)
        return 1
    print('Native Hermes Google handoff acceptance passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
