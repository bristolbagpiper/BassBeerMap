"""Stage and gate a PDF directory import; never write production files here."""
import argparse
import json
import shutil
from pathlib import Path
from urllib.request import Request, urlopen

from coordinate_sources import resolve_new_pin
from directory_release import (bootstrap_registry, read_json, read_rows, reconcile,
                               validate_release, write_json, write_release, venue_key)
from build_change_report import build_report
from build_coordinate_review_queue import build_queue
import csv


def postcode_coordinates(rows, existing):
    coordinates = {postcode: value for postcode, value in existing.items() if postcode in {row['postcode'] for row in rows}}
    missing = sorted({row['postcode'] for row in rows} - set(coordinates))
    for offset in range(0, len(missing), 100):
        request = Request('https://api.postcodes.io/postcodes',
                          data=json.dumps({'postcodes': missing[offset:offset + 100]}).encode(),
                          headers={'Content-Type': 'application/json', 'User-Agent': 'BassBeerMapBot/1.0'})
        with urlopen(request, timeout=30) as response:
            results = json.load(response)['result']
        for result in results:
            value = result.get('result') or {}
            if value.get('latitude') is not None and value.get('longitude') is not None:
                coordinates[result['query']] = {'lat': value['latitude'], 'lng': value['longitude']}
    return coordinates


def stage_import(candidate, root, staging, check_sources=True):
    root, staging = Path(root), Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    report = {'valid': False, 'errors': [], 'source_warnings': []}
    try:
        raw = read_rows(candidate)
        previous = read_rows(root / 'pubs.csv')
        decisions = read_json(root / 'ingestion-decisions.json', {})
        rows, registry, reconciliation = reconcile(raw, bootstrap_registry(root), decisions)
        write_json(staging / 'reconciliation-report.json', reconciliation)
        if reconciliation['errors']:
            report['errors'].extend(reconciliation['errors'])
            return report
        # Validate listing content before spending time on external enrichment.
        from validate_csv import validate
        valid, errors, _ = validate(previous, rows)
        if not valid:
            report['errors'].extend(errors)
            return report
        coordinates = postcode_coordinates(rows, read_json(root / 'pub-coordinates.json', {'coordinates': {}})['coordinates'])
        if check_sources:
            for row in rows:
                entry = registry['venues'][row['venue_id']]
                if entry.get('verified_pin'):
                    continue
                attempt = {}
                try:
                    pin = resolve_new_pin(row, attempt)
                    if pin:
                        entry['verified_pin'] = pin
                except Exception as error:
                    attempt['error'] = type(error).__name__
                    report['source_warnings'].append(f'{venue_key(row)}: location lookup unavailable: {type(error).__name__}')
                entry['verification_attempt'] = attempt
        metadata = read_json(root / 'candidate-directory-meta.json', read_json(root / 'directory-meta.json', {}))
        ledger = write_release(staging, rows, registry, metadata, coordinates)
        pdf = root / 'latest-bass-directory.pdf'
        if pdf.exists():
            shutil.copyfile(pdf, staging / pdf.name)
        change_report = build_report(previous, rows)
        write_json(staging / 'change-report.json', change_report)
        verified = {k: r for k, r in ledger['records'].items() if r['display_precision'] == 'venue'}
        queue = build_queue(rows, verified, {})
        with (staging / 'coordinate-review-queue.csv').open('w', encoding='utf-8', newline='') as handle:
            from build_coordinate_review_queue import FIELDS
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(queue)
        report['errors'].extend(validate_release(staging, root))
        report['summary'] = dict(change_report['summary'], verified_pins=len(verified), approximate_pins=len(queue))
        report['valid'] = not report['errors']
        return report
    except Exception as error:
        report['errors'].append(f'{type(error).__name__}: {error}')
        return report
    finally:
        write_json(staging / 'validation-report.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('candidate')
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--staging', type=Path, default=Path('candidate-release'))
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    report = stage_import(args.candidate, args.root, args.staging, not args.offline)
    print(json.dumps(report, indent=2))
    raise SystemExit(not report['valid'])
