"""Read-only coordinate comparison; report uncertainty without moving any pin."""
import argparse
import copy
import csv
import json
import shutil
from collections import Counter
from datetime import date
from pathlib import Path

from directory_release import RELEASE_FILES, distance_metres, point_valid, read_json, read_rows, validate_release, write_json, write_release
from resolve_venue_coordinates_from_fhrs import search, choose_match, names_match


def checkpoint(path, checks):
    temporary = path.with_suffix('.json.tmp')
    write_json(temporary, {'checks': checks})
    temporary.replace(path)


def listing_signature(row):
    return {key: row.get(key) for key in ('pub_name', 'postcode')}


def pin_signature(pin):
    return {key: pin.get(key) for key in ('lat', 'lng')}


def compare(row, pin, candidates):
    same_postcode = [c for c in candidates if str(c.get('PostCode', '')).replace(' ', '').upper() == row['postcode'].replace(' ', '').upper()]
    named = [c for c in same_postcode if names_match(row['pub_name'], c.get('BusinessName', ''))]
    match = choose_match(row, same_postcode)
    if not match:
        return {'status': 'ambiguous_or_missing_coordinates' if named else 'no_name_match', 'postcode_matches': len(same_postcode), 'name_matches': len(named)}
    business = next(c for c in named if c.get('FHRSID') == match['fhrs_id'])
    difference = round(distance_metres(pin, match), 1)
    return dict(status='coordinate_disagreement' if difference > 50 else 'nearby' if difference > 25 else 'coordinate_agreement',
                difference_metres=difference, lat=match['lat'], lng=match['lng'],
                name=business['BusinessName'], postcode=business['PostCode'],
                address=', '.join(str(business.get(f'AddressLine{i}') or '') for i in range(1, 5)).strip(', '),
                url=f"https://ratings.food.gov.uk/business/{match['fhrs_id']}")


def assess(row, entry, lookup=search):
    pin = entry.get('verified_pin') or {}
    if not point_valid(pin):
        return dict(checked_at=date.today().isoformat(), pin=pin_signature(pin), listing=listing_signature(row),
                    status='no_accepted_pin', confidence='existing_evidence_needs_corroboration',
                    owner_reviewed=bool(entry.get('location_approvals')), independent_of_pin=False,
                    comparisons=[], reference={}, priority=0,
                    reasons=['No accepted venue coordinate yet; postcode pin remains approximate'])
    reference = pin.get('metadata_issue', {}).get('reference')
    postcodes = list(dict.fromkeys([row['postcode']] + ([reference] if reference else [])))
    comparisons = []
    for postcode in postcodes:
        query = dict(row, postcode=postcode)
        try:
            comparisons.append(dict(compare(query, pin, lookup(query, include_name=False)), queried_postcode=postcode))
        except Exception as error:
            comparisons.append(dict(status='unavailable', error=type(error).__name__, queried_postcode=postcode))
    # An agreeing reference-postcode result must not erase a listed-postcode conflict.
    usable = [c for c in comparisons if 'difference_metres' in c]
    result = min(usable, key=lambda c: c['difference_metres']) if usable else comparisons[0]
    source_urls = [e.get('url', '') for e in pin.get('evidence', [])]
    independent_of_pin = any(url and 'ratings.food.gov.uk' not in url for url in source_urls) and not any('ratings.food.gov.uk' in url for url in source_urls)
    owner_reviewed = bool(entry.get('location_approvals'))
    confidence = ('owner_reviewed' if owner_reviewed else
                  'independent_coordinate_agreement' if result['status'] == 'coordinate_agreement' and independent_of_pin else
                  'existing_evidence_needs_corroboration')
    reasons = []
    if reference:
        reasons.append('Directory postcode differs from reviewed reference postcode')
    if result['status'] == 'coordinate_disagreement':
        reasons.append(f"FSA named-address coordinate is {result['difference_metres']}m from the displayed pin; geocode may be approximate")
    elif result['status'] == 'nearby':
        reasons.append('FSA coordinate is 25-50m away; building-level review recommended')
    elif result['status'] not in ('coordinate_agreement',):
        reasons.append('Independent address/coordinate comparison is inconclusive')
    return dict(checked_at=date.today().isoformat(), pin=pin_signature(pin), listing=listing_signature(row), status=result['status'],
                confidence=confidence, owner_reviewed=owner_reviewed, independent_of_pin=independent_of_pin,
                comparisons=comparisons, reasons=reasons, reference=result,
                priority=0 if reference else 1 if result['status'] == 'coordinate_disagreement' else 2 if reasons else 3)


def select_rows(rows, registry, checks, limit):
    def priority(row):
        prior = checks.get(row['venue_id'], {})
        current_pin = registry['venues'][row['venue_id']].get('verified_pin') or {}
        if prior.get('pin') != pin_signature(current_pin) or prior.get('listing') != listing_signature(row):
            prior = {}
        issue = current_pin.get('metadata_issue')
        return (prior.get('checked_at', ''), not bool(issue), row['venue_id'])
    ordered = sorted(rows, key=priority)
    return ordered[:limit] if limit else ordered


def report(rows, registry, checks, output):
    records = []
    for row in rows:
        entry = registry['venues'][row['venue_id']]
        check = checks.get(row['venue_id'], {})
        if check.get('pin') != pin_signature(entry.get('verified_pin') or {}) or check.get('listing') != listing_signature(row):
            check = {'status': 'not_checked', 'priority': 0, 'reasons': ['Current pin has not been independently compared']}
        records.append(dict(row, **check))
    records.sort(key=lambda r: (r['priority'], r['pub_name'], r['venue_id']))
    summary = dict(total=len(rows), counts=dict(Counter(r['status'] for r in records)),
                   confidence_counts=dict(Counter(r.get('confidence', 'not_checked') for r in records)),
                   limitations=['Coordinate agreement does not prove the correct building or current Bass availability.',
                                'FSA geocodes may be postcode centres. Missing matches and outages are inconclusive.',
                                'No coordinates or owner approvals are changed by this audit.'])
    write_json(output / 'full-location-audit.json', dict(schema_version=1, summary=summary, records=records))
    fields = ['venue_id', 'pub_name', 'place_name', 'postcode', 'priority', 'status', 'confidence', 'checked_at', 'reasons']
    with (output / 'full-location-audit.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(dict(r, reasons='; '.join(r.get('reasons', []))) for r in records)
    lines = ['# Full location audit', '', f"Locations: {len(rows)}", '', '## Results', '',
             *[f'- {k}: {v}' for k, v in summary['counts'].items()], '', '## Confidence', '',
             *[f'- {k}: {v}' for k, v in summary['confidence_counts'].items()], '', '## Limits', '',
             *[f'- {value}' for value in summary['limitations']], '', '## Review order', '',
             '| Pub | Place | Postcode | Result | Reason |', '|---|---|---|---|---|']
    for r in records:
        if r['priority'] < 3:
            clean = lambda value: str(value).replace('|', '/').replace('\n', ' ')
            lines.append('| ' + ' | '.join(map(clean, [r['pub_name'], r['place_name'], r['postcode'], r['status'], '; '.join(r.get('reasons', []))])) + ' |')
    (output / 'full-location-audit.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary


def run(root, output, limit=0, publish=False, resume=False):
    root, output = Path(root), Path(output)
    errors = validate_release(root)
    assert not errors, errors
    output.mkdir(parents=True, exist_ok=True)
    rows = read_rows(root / 'pubs.csv')
    registry = read_json(root / 'venue-registry.json')
    checks = read_json(root / 'independent-audit-state.json', {}).get('checks', {})
    if resume:
        checks.update(read_json(output / 'independent-audit-state.json', {}).get('checks', {}))
    selected = select_rows(rows, registry, checks, limit)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pending = [row for row in selected if not (resume and checks.get(row['venue_id'], {}).get('checked_at') == date.today().isoformat() and checks.get(row['venue_id'], {}).get('pin') == pin_signature(registry['venues'][row['venue_id']].get('verified_pin') or {}) and checks.get(row['venue_id'], {}).get('listing') == listing_signature(row))]
    completed = len(selected) - len(pending)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(assess, row, registry['venues'][row['venue_id']]): row['venue_id'] for row in pending}
        for future in as_completed(futures):
            checks[futures[future]] = future.result()
            completed += 1
            checkpoint(output / 'independent-audit-state.json', checks)
            if completed % 25 == 0 or completed == len(selected):
                print(f'{completed}/{len(selected)} compared', flush=True)
    # Refresh derived confidence after resuming a report created by older code.
    for row in rows:
        identifier = row['venue_id']
        check = checks.get(identifier)
        if not check:
            continue
        entry = registry['venues'][identifier]
        urls = [e.get('url', '') for e in (entry.get('verified_pin') or {}).get('evidence', [])]
        independent = any(url and 'ratings.food.gov.uk' not in url for url in urls) and not any('ratings.food.gov.uk' in url for url in urls)
        check['independent_of_pin'] = independent
        check['owner_reviewed'] = bool(entry.get('location_approvals'))
        check['confidence'] = ('owner_reviewed' if check['owner_reviewed'] else 'independent_coordinate_agreement' if check['status'] == 'coordinate_agreement' and independent else 'existing_evidence_needs_corroboration')
        check['reasons'] = [reason.replace('25\ufffd50m', '25-50m') for reason in check.get('reasons', [])]
    checkpoint(output / 'independent-audit-state.json', checks)
    summary = report(rows, registry, checks, output)
    if publish:
        for row in rows:
            check = checks.get(row['venue_id'])
            if check:
                registry['venues'][row['venue_id']]['independent_location_check'] = copy.deepcopy(check)
        before = read_json(root / 'directory-release.json')
        write_release(output, rows, registry, read_json(root / 'directory-meta.json'), read_json(root / 'pub-coordinates.json')['coordinates'])
        for name in ('latest-bass-directory.pdf', 'venue-coordinate-overrides.json'):
            if (root / name).exists():
                shutil.copyfile(root / name, output / name)
        assert read_json(output / 'directory-release.json')['locations'] == before['locations'], 'Audit must never move or recolour pins'
        errors = validate_release(output, root)
        assert not errors, errors
        for name in RELEASE_FILES + ['independent-audit-state.json']:
            shutil.copyfile(output / name, root / name)
        (root / 'audit').mkdir(exist_ok=True)
        for name in ('full-location-audit.json', 'full-location-audit.csv', 'full-location-audit.md'):
            shutil.copyfile(output / name, root / 'audit' / name)
        assert not validate_release(root)
    print(json.dumps(summary, indent=2), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--output', default='candidate-independent')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    run(args.root, args.output, args.limit, args.publish, args.resume)
