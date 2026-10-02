"""Find review candidates independently of FSA; never promote a fuzzy match."""
import argparse
import copy
import re
import shutil
import time
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from coordinate_sources import camra_page, name_matches, resolve_new_pin
from directory_release import (RELEASE_FILES, distance_metres, point_valid, read_json,
                               read_rows, validate_release, write_json, write_release)


def candidate_score(wanted, actual):
    clean = lambda name: re.sub(r'[^a-z0-9]', '', re.sub(r'^the\s+', '', name.casefold()))
    left, right = clean(wanted), clean(actual)
    if name_matches(wanted, actual):
        return 3
    if min(len(left), len(right)) >= 5 and (right.startswith(left) or left.startswith(right)):
        return 2
    return 1 if min(len(left), len(right)) >= 6 and SequenceMatcher(None, left, right).ratio() >= .78 else 0


def discover(row, centre):
    from audit.fetch_camra import fetch
    result = fetch((centre['lat'] - .05, centre['lat'] + .05, centre['lng'] - .08, centre['lng'] + .08))
    proposals = []
    for item in result['venues']:
        if item.get('PremisesStatus') == 'X':
            continue
        score = candidate_score(row['pub_name'], str(item.get('Name') or ''))
        if not score:
            continue
        try:
            point = dict(lat=float(item['Latitude']), lng=float(item['Longitude']))
        except (KeyError, TypeError, ValueError):
            continue
        if not point_valid(point) or distance_metres(centre, point) > 6000:
            continue
        postcode = str(item.get('Postcode') or '')
        proposals.append(dict(point, name=item['Name'], postcode=postcode, address=item.get('Street') or '',
                              town=item.get('Town') or item.get('Posttown') or '',
                              url=f"https://camra.org.uk/pubs/{item['IncID']}", score=score,
                              postcode_agrees=postcode.replace(' ', '').upper() == row['postcode'].replace(' ', '').upper(),
                              distance_from_postcode_metres=round(distance_metres(centre, point))))
    proposals = list({p['url']: p for p in proposals}.values())
    proposals.sort(key=lambda p: (-p['score'], not p['postcode_agrees'], p['distance_from_postcode_metres'], p['url']))
    detailed = []
    for proposal in proposals[:3]:
        try:
            source = camra_page(proposal['url'])
            if not candidate_score(row['pub_name'], source['name']) or distance_metres(centre, source) > 6000:
                continue
            proposal.update(source)
            proposal['postcode_agrees'] = source['postcode'].replace(' ', '').upper() == row['postcode'].replace(' ', '').upper()
            proposal['distance_from_postcode_metres'] = round(distance_metres(centre, source))
            proposal['source_status'] = 'named_page_checked'
        except Exception as error:
            proposal['source_status'] = 'page_unavailable'
            proposal['source_error'] = type(error).__name__
        detailed.append(proposal)
    return detailed, {'records_returned': len(result['venues']), 'reported_total': result.get('total'),
                           'complete': result.get('total', len(result['venues'])) <= len(result['venues'])}


def research(root, staging, limit=30):
    root, staging = Path(root), Path(staging)
    errors = validate_release(root)
    if errors:
        raise ValueError('; '.join(errors))
    rows = read_rows(root / 'pubs.csv')
    registry = copy.deepcopy(read_json(root / 'venue-registry.json'))
    ledger = read_json(root / 'coordinate-verification.json')
    postcodes = read_json(root / 'pub-coordinates.json')['coordinates']
    unresolved = [r for r in rows if ledger['records'][registry['venues'][r['venue_id']]['current_key']]['display_precision'] != 'venue']
    unresolved.sort(key=lambda r: (registry['venues'][r['venue_id']].get('location_research', {}).get('checked_at', ''), r['venue_id']))
    report = {'checked': [], 'automatically_verified': [], 'warnings': []}
    for row in unresolved[:limit]:
        identifier = row['venue_id']
        entry = registry['venues'][identifier]
        # Disputed historic pins require explicit human review, even if an
        # automatic lookup happens to agree with one source again.
        if not entry.get('verified_pin'):
            attempt = {}
            try:
                pin = resolve_new_pin(row, attempt)
                if pin:
                    entry['verified_pin'] = pin
                    report['automatically_verified'].append(identifier)
            except Exception as error:
                attempt['error'] = type(error).__name__
            entry['verification_attempt'] = attempt
        if identifier not in report['automatically_verified']:
            try:
                proposals, coverage = discover(row, postcodes[row['postcode']])
                entry['location_research'] = dict(checked_at=date.today().isoformat(), status='candidates_found' if proposals else 'no_candidate',
                                                 proposals=proposals, coverage=coverage)
            except Exception as error:
                # Preserve earlier candidate evidence during source outages.
                previous = entry.get('location_research', {})
                entry['location_research'] = dict(previous, checked_at=date.today().isoformat(), status='unavailable', error=type(error).__name__)
                report['warnings'].append(f'{identifier}: CAMRA discovery unavailable ({type(error).__name__})')
        report['checked'].append(identifier)
        time.sleep(1)
    staging.mkdir(parents=True, exist_ok=True)
    write_release(staging, rows, registry, read_json(root / 'directory-meta.json'), postcodes)
    shutil.copyfile(root / 'latest-bass-directory.pdf', staging / 'latest-bass-directory.pdf')
    errors = validate_release(staging, root)
    report.update(valid=not errors, errors=errors)
    write_json(staging / 'research-report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--staging', default='candidate-research')
    parser.add_argument('--limit', type=int, default=30)
    args = parser.parse_args()
    report = research(args.root, args.staging, args.limit)
    print(f"Researched {len(report['checked'])}; automatically verified {len(report['automatically_verified'])}; warnings {len(report['warnings'])}")
    if report['errors']:
        print(report['errors'])
    raise SystemExit(not report['valid'])
