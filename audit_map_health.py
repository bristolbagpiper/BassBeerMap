"""Audit the live release and rotate fresh source checks without guessing pins."""
import argparse
import copy
import hashlib
import json
import shutil
import time
from datetime import date
from pathlib import Path

from coordinate_sources import fetch_text, recheck_pin, resolve_new_pin
from directory_release import (RELEASE_FILES, evidence_valid, read_json, read_rows,
                               validate_release, write_json, write_release)


def select_batch(rows, registry, state, limit):
    def priority(row):
        identifier = row['venue_id']
        previous = state.get('checks', {}).get(identifier, {})
        pin = registry['venues'][identifier].get('verified_pin')
        checked = max((e.get('checked_at', '') for e in (pin or {}).get('evidence', [])), default='')
        return (previous.get('attempted_at', ''), checked, identifier)
    unresolved = sorted([r for r in rows if not registry['venues'][r['venue_id']].get('verified_pin')], key=priority)
    verified = sorted([r for r in rows if registry['venues'][r['venue_id']].get('verified_pin')], key=priority)
    # Unresolved venues must not starve the rotation through existing pins.
    first = unresolved[:min(10, limit)]
    selected = first + verified[:limit - len(first)]
    return selected + unresolved[len(first):len(first) + limit - len(selected)]


def apply_check(entry, result):
    pin = entry.get('verified_pin')
    if result['status'] == 'confirmed' and pin and not pin.get('review_required'):
        evidence = result['evidence']
        pin['evidence'] = [e for e in pin['evidence'] if e['url'] != evidence['url']] + [evidence]
    elif result['status'] == 'conflict' and pin:
        # Keep the historical coordinate and evidence, but stop displaying it
        # as precise. Restoration requires a reviewed registry correction.
        pin['review_required'] = True
        pin['review_reason'] = result['reason']


def health_audit(root, staging, limit=30, check_live=True):
    root, staging = Path(root), Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    # Ageing is handled below, not mistaken for corrupt committed assets.
    errors = validate_release(root, allow_expired=True)
    report = {'valid': not errors, 'errors': errors, 'issues': [], 'checked': [], 'source_warnings': []}
    if errors:
        write_json(staging / 'health-report.json', report)
        return report
    registry = copy.deepcopy(read_json(root / 'venue-registry.json'))
    rows = read_rows(root / 'pubs.csv')
    state = read_json(root / 'coordinate-health-state.json', {'checks': {}})
    previous_fingerprint = state.get('issue_fingerprint', '')
    if check_live:
        try:
            live = json.loads(fetch_text('https://bassbeermap.com/directory-release.json'))
            if live.get('release_id') != read_json(root / 'directory-release.json')['release_id']:
                report['issues'].append('The live map is not serving the committed release')
            if live.get('schema_version') != 1:
                report['issues'].append('The live map release schema is invalid')
        except Exception as error:
            report['issues'].append(f'The live map release could not be checked: {type(error).__name__}')
    for row in select_batch(rows, registry, state, limit):
        identifier = row['venue_id']
        entry = registry['venues'][identifier]
        try:
            if entry.get('verified_pin'):
                result = recheck_pin(row, entry['verified_pin'])
                apply_check(entry, result)
            else:
                attempt = {}
                try:
                    pin = resolve_new_pin(row, attempt)
                finally:
                    entry['verification_attempt'] = attempt
                result = {'status': 'confirmed' if pin else 'unresolved'}
                if pin:
                    entry['verified_pin'] = pin
        except Exception as error:
            result = {'status': 'unavailable', 'reason': type(error).__name__}
            report['source_warnings'].append(f'{identifier}: {type(error).__name__}; last verified pin retained')
        state['checks'][identifier] = dict(result, attempted_at=date.today().isoformat())
        if entry.get('verified_pin'):
            sources = {e['source']: {'status': result['status'], 'url': e['url']}
                       for e in entry['verified_pin'].get('evidence', [])}
            entry['verification_attempt'] = dict(result, checked_at=date.today().isoformat(), sources=sources)
        report['checked'].append({'venue_id': identifier, 'pub_name': row['pub_name'], 'status': result['status']})
        time.sleep(1)
    for row in rows:
        identifier = row['venue_id']
        pin = registry['venues'][identifier].get('verified_pin')
        result = state['checks'].get(identifier, {})
        if pin and pin.get('review_required'):
            report['issues'].append(f'{identifier}: {row["pub_name"]}: {pin["review_reason"]}')
        elif pin and not evidence_valid(pin):
            pin['review_required'] = True
            pin['review_reason'] = 'Coordinate evidence expired or is incomplete'
            report['issues'].append(f'{identifier}: {row["pub_name"]}: coordinate evidence needs review')
        elif not pin:
            report['issues'].append(f'{identifier}: {row["pub_name"]}: approximate postcode pin needs venue verification')
        elif result.get('status') == 'manual_review':
            newest = max(date.fromisoformat(e['checked_at']) for e in pin['evidence'])
            if (date.today() - newest).days >= 180:
                report['issues'].append(f'{identifier}: {row["pub_name"]}: manual evidence needs a fresh address/map check')
        elif result.get('status') == 'unavailable':
            report['issues'].append(f'{identifier}: {row["pub_name"]}: verification source unavailable ({result["reason"]})')
        issue = (pin or {}).get('metadata_issue', {})
        if issue and issue.get('listed', '').replace(' ', '').upper() != issue.get('reference', '').replace(' ', '').upper():
            report['issues'].append(f'{identifier}: {row["pub_name"]}: directory postcode {issue["listed"]} conflicts with verified address {issue["reference"]}')
    write_release(staging, rows, registry, read_json(root / 'directory-meta.json'), read_json(root / 'pub-coordinates.json')['coordinates'])
    if (root / 'latest-bass-directory.pdf').exists():
        shutil.copyfile(root / 'latest-bass-directory.pdf', staging / 'latest-bass-directory.pdf')
    errors = validate_release(staging, root, allow_review_downgrades=True)
    report['errors'].extend(errors)
    report['valid'] = not report['errors']
    fingerprint = hashlib.sha256(json.dumps(sorted(report['issues'])).encode()).hexdigest()
    report['notify'] = bool((report['issues'] or previous_fingerprint) and fingerprint != previous_fingerprint)
    state['issue_fingerprint'] = fingerprint
    write_json(staging / 'coordinate-health-state.json', state)
    write_json(staging / 'health-report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--staging', type=Path, default=Path('candidate-health'))
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--skip-live', action='store_true')
    args = parser.parse_args()
    report = health_audit(args.root, args.staging, args.limit, not args.skip_live)
    print(json.dumps(report, indent=2))
    raise SystemExit(not report['valid'])
