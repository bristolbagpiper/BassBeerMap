"""Audit the live release and rotate fresh source checks without guessing pins."""
import argparse
import copy
import hashlib
import json
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path

from coordinate_sources import fetch_text, recheck_pin, resolve_new_pin
from directory_release import (RELEASE_FILES, active_evidence, evidence_valid, read_json, read_rows,
                               validate_release, write_json, write_release)


def select_batch(rows, registry, state, limit):
    def priority(row):
        identifier = row['venue_id']
        previous = state.get('checks', {}).get(identifier, {})
        pin = registry['venues'][identifier].get('verified_pin')
        checked = max((e.get('checked_at', '') for e in active_evidence(pin)), default='')
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
        evidence = copy.deepcopy(result['evidence'])
        if not active_evidence({'evidence': [evidence]}):
            raise ValueError('Confirmed source evidence must be active')
        previous = pin.get('evidence', [])
        replaced = [e for e in active_evidence(pin) if e.get('url') == evidence['url']]
        retained = [e for e in previous if e not in replaced]
        for item in replaced:
            if item != evidence:
                historical = dict(item, disposition='superseded_by_source_refresh')
                if historical not in retained:
                    retained.append(historical)
        pin['evidence'] = retained + [evidence]
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
    report = {'valid': not errors, 'healthy': not errors, 'errors': errors, 'issues': [],
              'live_issues': [], 'checked': [], 'source_warnings': []}
    if errors:
        write_json(staging / 'health-report.json', report)
        return report
    registry = copy.deepcopy(read_json(root / 'venue-registry.json'))
    rows = read_rows(root / 'pubs.csv')
    postcode_coordinates = read_json(root / 'pub-coordinates.json')['coordinates']
    state = read_json(root / 'coordinate-health-state.json', {'checks': {}})
    previous_fingerprint = state.get('issue_fingerprint', '')
    if check_live:
        try:
            live = json.loads(fetch_text('https://bassbeermap.com/directory-release.json'))
            if live.get('release_id') != read_json(root / 'directory-release.json')['release_id']:
                report['live_issues'].append('The live map is not serving the committed release')
            if live.get('schema_version') != 1:
                report['live_issues'].append('The live map release schema is invalid')
        except Exception as error:
            report['live_issues'].append(f'The live map release could not be checked: {type(error).__name__}')
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
                    pin = resolve_new_pin(row, attempt, centre=postcode_coordinates.get(row['postcode']))
                finally:
                    entry['verification_attempt'] = attempt
                for warning in attempt.get('source_warnings', []):
                    report['source_warnings'].append(f'{identifier}: {warning}')
                result = {'status': 'confirmed' if pin else 'unresolved'}
                if pin:
                    entry['verified_pin'] = pin
        except Exception as error:
            result = {'status': 'unavailable', 'reason': type(error).__name__}
        if result['status'] == 'unavailable':
            retained = 'last verified pin retained' if entry.get('verified_pin') else 'location remains approximate'
            report['source_warnings'].append(f'{identifier}: {result.get("reason", "unknown error")}; {retained}')
        state['checks'][identifier] = dict(result, attempted_at=date.today().isoformat())
        if entry.get('verified_pin'):
            checked_url = (result.get('evidence') or {}).get('url') or result.get('source_url')
            sources = {}
            for item in active_evidence(entry['verified_pin']):
                source_status = result['status'] if checked_url and item['url'] == checked_url else 'not_checked'
                # A provider can have several retained readings. Prefer the
                # specific checked URL over another unqueried reference.
                if item['source'] not in sources or source_status != 'not_checked':
                    sources[item['source']] = {'status': source_status, 'url': item['url']}
            entry['verification_attempt'] = dict(result, checked_at=date.today().isoformat(), sources=sources)
        else:
            # Retain lookup diagnostics, including an outage before any pin exists.
            entry.setdefault('verification_attempt', {}).update(result, checked_at=date.today().isoformat())
        report['checked'].append({'venue_id': identifier, 'pub_name': row['pub_name'], 'status': result['status']})
        time.sleep(1)
    for row in rows:
        identifier = row['venue_id']
        pin = registry['venues'][identifier].get('verified_pin')
        if pin and not pin.get('review_required') and not evidence_valid(pin):
            pin['review_required'] = True
            pin['review_reason'] = 'Coordinate evidence expired or is incomplete'
    write_release(staging, rows, registry, read_json(root / 'directory-meta.json'), postcode_coordinates)
    # Share the queue's human-review rules; accepted postcode and FSA differences
    # must not turn into a second, inconsistent set of health alerts.
    review = read_json(staging / 'location-review.json')
    report['issues'] = report['live_issues'] + [
        f'{identifier}: {record["pub_name"]}: {reason}'
        for identifier, record in review['records'].items() if record['active']
        for reason in record['reasons']]
    if (root / 'latest-bass-directory.pdf').exists():
        shutil.copyfile(root / 'latest-bass-directory.pdf', staging / 'latest-bass-directory.pdf')
    errors = validate_release(staging, root, allow_review_downgrades=True)
    report['errors'].extend(errors)
    report['valid'] = not report['errors']
    report['healthy'] = not (report['errors'] or report['live_issues'])
    fingerprint = hashlib.sha256(json.dumps(sorted(report['issues'])).encode()).hexdigest()
    report['notify'] = bool((report['issues'] or previous_fingerprint) and fingerprint != previous_fingerprint)
    state['issue_fingerprint'] = fingerprint
    write_json(staging / 'coordinate-health-state.json', state)
    write_json(staging / 'health-report.json', report)
    return report


def write_health_status(staging, destination, run_id='', run_url='', pipeline_failed=False, run_attempt=1):
    """Persist deployment failures even when candidate release validation failed.

    This file is deliberately outside the release bundle. Notification reruns
    read the latest main-branch snapshot, never a triggering run's old artifact.
    """
    report = read_json(Path(staging) / 'health-report.json')
    if report is None:
        issues = ['Map audit failed before producing its report']
    else:
        issues = report.get('errors', []) + report.get('live_issues', [])
    if pipeline_failed and not issues:
        issues.append('Map audit pipeline failed; inspect its validation or publication step')
    status = dict(schema_version=1, issues=issues, checked_at=datetime.now(timezone.utc).isoformat(),
                  run_id=str(run_id), run_attempt=int(run_attempt), run_url=run_url)
    write_json(destination, status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--staging', type=Path, default=Path('candidate-health'))
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--skip-live', action='store_true')
    args = parser.parse_args(argv)
    report = health_audit(args.root, args.staging, args.limit, not args.skip_live)
    print(json.dumps(report, indent=2))
    return int(not report['healthy'])


if __name__ == '__main__':
    raise SystemExit(main())
