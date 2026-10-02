"""Reconcile venue identity and build one internally consistent map release.

The registry owns permanent IDs and evidence-backed pins. A PDF supplies listing
metadata, never permission to move a previously reviewed venue.
"""
import copy
import csv
import hashlib
import json
import math
import re
import uuid
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from build_coordinate_verification import venue_key
from validate_csv import validate as validate_csv

MAX_EVIDENCE_AGE_DAYS = 400
FIELDS = ['country', 'area', 'pub_name', 'place_name', 'postcode', 'pg', 'last', 'dispense', 'notes']
RELEASE_FILES = ['pubs.csv', 'directory-meta.json', 'pub-coordinates.json',
                 'venue-coordinates.json', 'coordinate-verification.json',
                 'venue-registry.json', 'directory-release.json', 'location-review.json', 'latest-bass-directory.pdf']


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else copy.deepcopy(default)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')


def read_rows(path):
    with Path(path).open(encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle))


def write_rows(path, rows):
    with Path(path).open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS + ['venue_id'], lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def permanent_id(key):
    # Used once at creation. Future metadata changes retain the stored ID.
    return 'v-' + str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://bassbeermap.com/venue/' + key))


def point_valid(value):
    try:
        if any(not isinstance(value[field], (int, float)) or isinstance(value[field], bool) for field in ('lat', 'lng')):
            return False
        lat, lng = float(value['lat']), float(value['lng'])
        return math.isfinite(lat) and math.isfinite(lng) and 49 <= lat <= 61 and -9 <= lng <= 3
    except (KeyError, TypeError, ValueError):
        return False


def distance_metres(left, right):
    lat1, lng1, lat2, lng2 = map(math.radians, (left['lat'], left['lng'], right['lat'], right['lng']))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(a)))


def evidence_valid(pin, today=None, allow_expired=False):
    today = today or date.today()
    if not point_valid(pin) or not pin.get('evidence') or pin.get('review_required'):
        return False
    valid = []
    for item in pin['evidence']:
        try:
            checked = date.fromisoformat(item['checked_at'])
            reference = item.get('reference_coordinate')
            valid.append(bool(item.get('source') and str(item.get('url', '')).startswith('https://')
                              and reference and point_valid(reference) and distance_metres(pin, reference) <= 50
                              and 0 <= (today - checked).days
                              and (allow_expired or (today - checked).days <= MAX_EVIDENCE_AGE_DAYS)))
        except (KeyError, TypeError, ValueError):
            valid.append(False)
    return any(valid)


def bootstrap_registry(root):
    """Migrate the existing reviewed ledger without reinterpreting OSM candidates."""
    root = Path(root)
    existing = read_json(root / 'venue-registry.json', None)
    if existing is not None:
        return existing
    registry = {'schema_version': 1, 'venues': {}}
    ledger = read_json(root / 'coordinate-verification.json', {'records': {}})['records']
    for key, record in ledger.items():
        identifier = permanent_id(key)
        pin = None
        if record.get('status') == 'verified_venue' and point_valid(record):
            pin = {field: copy.deepcopy(record[field]) for field in ('lat', 'lng', 'evidence')}
            pin['source'] = record.get('coordinate_source', '')
            for item in pin['evidence']:
                # Historical audit evidence attests to this exact ledger point;
                # preserve the point so later resolver changes cannot reuse it.
                item['reference_coordinate'] = {'lat': record['lat'], 'lng': record['lng']}
                item['reference_postcode'] = item.get('reference_postcode') or record['postcode']
            if record.get('metadata_issue'):
                pin['metadata_issue'] = copy.deepcopy(record['metadata_issue'])
        registry['venues'][identifier] = {
            'aliases': [key], 'current_key': key,
            'listing': {field: record.get(field, '') for field in ('pub_name', 'place_name', 'postcode')},
            'verified_pin': pin,
        }
    return registry


def reconcile(rows, registry, decisions):
    """Only exact known identities or explicit reviewed aliases retain an ID."""
    registry = copy.deepcopy(registry)
    errors, changes, ignored = [], [], []
    groups = defaultdict(list)
    for original in rows:
        row = {field: str(original.get(field, '')).strip() for field in FIELDS}
        groups[venue_key(row)].append(row)
    selected = []
    for key, duplicates in groups.items():
        unique = list({json.dumps(row, sort_keys=True): row for row in duplicates}.values())
        if len(unique) > 1:
            decision = decisions.get('duplicate_resolutions', {}).get(key, {})
            approved = decision.get('accepted')
            rejected = decision.get('rejected', [])
            if not approved or not decision.get('reason') or not decision.get('source_url') or not decision.get('checked_at'):
                errors.append(f'{key}: conflicting duplicate listings require review')
                continue
            matches = lambda row, subset: all(row.get(k) == v for k, v in subset.items())
            winners = [row for row in unique if matches(row, approved)]
            losers = [row for row in unique if not matches(row, approved)]
            if len(winners) != 1 or any(not any(matches(row, subset) for subset in rejected) for row in losers):
                errors.append(f'{key}: duplicate changed beyond the reviewed exception')
                continue
            selected.append(winners[0])
            ignored.append({'venue_key': key, 'reason': decision['reason'], 'rejected': losers})
        else:
            selected.append(unique[0])
            if len(duplicates) > 1:
                ignored.append({'venue_key': key, 'reason': 'identical duplicate', 'count': len(duplicates) - 1})

    aliases = {}
    for identifier, entry in registry['venues'].items():
        for alias in entry['aliases']:
            if alias in aliases and aliases[alias] != identifier:
                errors.append(f'{alias}: registry alias belongs to multiple venues')
            aliases[alias] = identifier
    claimed = set()
    active_keys = {venue_key(row) for row in selected}
    for row in selected:
        key = venue_key(row)
        identifier = aliases.get(key)
        decision = decisions.get('identity_aliases', {}).get(key)
        if decision and identifier is None:
            identifier = aliases.get(decision.get('previous_key'))
            if not identifier or not all(decision.get(f) for f in ('reason', 'source_url', 'checked_at', 'reference_postcode')):
                errors.append(f'{key}: identity alias has incomplete review evidence')
                continue
            entry = registry['venues'][identifier]
            entry['aliases'].append(key)
            entry['identity_reviews'] = entry.get('identity_reviews', []) + [copy.deepcopy(decision)]
            # A reviewed correction to listing metadata is not a coordinate move.
            if entry.get('verified_pin') and row['postcode'] != decision['reference_postcode']:
                entry['verified_pin']['metadata_issue'] = {
                    'field': 'postcode', 'listed': row['postcode'],
                    'reference': decision['reference_postcode'], 'source_url': decision['source_url'],
                }
            if entry.get('verified_pin') and row['postcode'] == decision['reference_postcode']:
                entry['verified_pin'].pop('metadata_issue', None)
        if identifier is None:
            similar = []
            for old_id, entry in registry['venues'].items():
                old = entry['listing']
                same_place = old.get('place_name', '').casefold() == row['place_name'].casefold()
                same_name = old.get('pub_name', '').casefold() == row['pub_name'].casefold()
                same_postcode = old.get('postcode', '').casefold() == row['postcode'].casefold()
                if same_place and (same_name or same_postcode) and entry['current_key'] not in active_keys:
                    similar.append(old_id)
            if similar:
                errors.append(f'{key}: possible rename/postcode change requires identity review ({", ".join(similar)})')
                continue
            identifier = permanent_id(key)
            registry['venues'][identifier] = {'aliases': [key], 'current_key': key, 'listing': row, 'verified_pin': None}
            changes.append({'venue_id': identifier, 'type': 'new', 'venue_key': key})
        entry = registry['venues'][identifier]
        if identifier in claimed:
            errors.append(f'{key}: multiple active listings resolve to one permanent venue ID')
            continue
        claimed.add(identifier)
        if entry['current_key'] != key:
            changes.append({'venue_id': identifier, 'type': 'identity_change', 'previous_key': entry['current_key'], 'venue_key': key})
        entry['current_key'] = key
        entry['listing'] = copy.deepcopy(row)
        issue = (entry.get('verified_pin') or {}).get('metadata_issue')
        if issue:
            compact = lambda value: str(value).replace(' ', '').upper()
            if compact(row['postcode']) == compact(issue.get('reference')):
                entry['verified_pin'].pop('metadata_issue', None)
            else:
                issue['listed'] = row['postcode']
        row['venue_id'] = identifier
    return selected, registry, {'errors': errors, 'changes': changes, 'duplicate_resolutions': ignored}


def build_ledger(rows, registry, allow_expired=False):
    records = {}
    for row in rows:
        entry = registry['venues'][row['venue_id']]
        pin = entry.get('verified_pin')
        verified = bool(pin and evidence_valid(pin, allow_expired=allow_expired))
        record = {
            'venue_id': row['venue_id'], 'pub_name': row['pub_name'], 'place_name': row['place_name'], 'postcode': row['postcode'],
            'status': 'verified_venue' if verified else 'venue_coordinate_requires_review' if pin else 'postcode_approximate',
            'display_precision': 'venue' if verified else 'postcode',
            'coordinate_source': pin.get('source', '') if pin else 'postcode',
            'evidence': copy.deepcopy(pin.get('evidence', [])) if pin else [],
        }
        if pin:
            record.update(lat=pin['lat'], lng=pin['lng'])
            if pin.get('metadata_issue'):
                record['metadata_issue'] = dict(pin['metadata_issue'], listed=row['postcode'])
        records[venue_key(row)] = record
    evidence_dates = [e['checked_at'] for r in records.values() for e in r['evidence'] if e.get('checked_at')]
    return {
        'schema_version': 2, 'generated_at': max(evidence_dates, default=''),
        'policy': {'max_evidence_age_days': MAX_EVIDENCE_AGE_DAYS, 'venue_pin': 'Evidence-backed venue coordinates; unresolved venues use explicitly approximate postcode pins.'},
        'counts': dict(Counter(r['status'] for r in records.values())), 'records': records,
    }


def build_bundle(rows, metadata, postcodes, ledger):
    locations = {}
    for row in rows:
        record = ledger['records'][venue_key(row)]
        precision = record['display_precision']
        point = record if precision == 'venue' else postcodes.get(row['postcode'])
        if not point_valid(point or {}):
            raise ValueError(f'{venue_key(row)}: no usable {precision} location')
        locations[row['venue_id']] = {'lat': point['lat'], 'lng': point['lng'], 'precision': precision}
        fallback = postcodes.get(row['postcode'])
        if not point_valid(fallback or {}):
            raise ValueError(f'{venue_key(row)}: postcode fallback is invalid or unavailable')
        locations[row['venue_id']]['postcode_location'] = fallback
        if precision == 'venue':
            locations[row['venue_id']]['checked_at'] = max(e['checked_at'] for e in record['evidence'])
        if record.get('metadata_issue'):
            locations[row['venue_id']]['metadata_issue'] = record['metadata_issue']
    payload = {'schema_version': 1, 'metadata': metadata, 'rows': rows, 'locations': locations,
               'max_evidence_age_days': MAX_EVIDENCE_AGE_DAYS}
    payload['release_id'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return payload


def write_release(root, rows, registry, metadata, postcodes):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = build_ledger(rows, registry)
    from location_review import build_review
    registry['location_review'] = build_review(rows, registry, ledger)
    write_json(root / 'location-review.json', registry['location_review'])
    venues = {venue_key(row): {k: v for k, v in registry['venues'][row['venue_id']]['verified_pin'].items() if k in ('lat', 'lng', 'source')}
              for row in rows if registry['venues'][row['venue_id']].get('verified_pin')}
    write_rows(root / 'pubs.csv', rows)
    write_json(root / 'venue-registry.json', registry)
    write_json(root / 'directory-meta.json', metadata)
    write_json(root / 'pub-coordinates.json', {'coordinates': postcodes, 'unmapped_postcodes': []})
    write_json(root / 'venue-coordinates.json', {'venues': venues, 'unresolved_venues': [k for k, r in ledger['records'].items() if r['display_precision'] != 'venue']})
    write_json(root / 'coordinate-verification.json', ledger)
    write_json(root / 'directory-release.json', build_bundle(rows, metadata, postcodes, ledger))
    return ledger


def validate_release(root, previous_root=None, allow_review_downgrades=False, allow_expired=False):
    root = Path(root)
    rows = read_rows(root / 'pubs.csv')
    registry = read_json(root / 'venue-registry.json')
    metadata = read_json(root / 'directory-meta.json')
    postcodes = read_json(root / 'pub-coordinates.json')['coordinates']
    ledger = read_json(root / 'coordinate-verification.json')
    bundle = read_json(root / 'directory-release.json')
    errors = []
    review = read_json(root / 'location-review.json')
    if review != registry.get('location_review') or review is None:
        errors.append('Location review register is missing or disagrees with the registry')
    elif not allow_expired:
        expected = {record['venue_id'] for record in ledger['records'].values() if record['display_precision'] != 'venue'}
        tracked = {identifier for identifier, record in review['records'].items() if record['active'] and record['unverified']}
        if expected != tracked:
            errors.append('Unverified locations are missing from the active review register')
    ids = [row.get('venue_id') for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        errors.append('Permanent venue IDs are missing or duplicated')
    keys = [venue_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        errors.append('Duplicate venue identities in candidate directory')
    if any(identifier not in registry['venues'] for identifier in ids):
        return errors + ['Directory references a missing registry venue']
    owned_aliases = {}
    for identifier, entry in registry['venues'].items():
        for alias in entry['aliases']:
            if alias in owned_aliases and owned_aliases[alias] != identifier:
                errors.append(f'{alias}: multiple permanent IDs claim the same identity')
            owned_aliases[alias] = identifier
    for row in rows:
        entry = registry['venues'][row['venue_id']]
        if entry['current_key'] != venue_key(row) or venue_key(row) not in entry['aliases']:
            errors.append(f'{row["venue_id"]}: registry does not agree with listing identity')
        if any(entry['listing'].get(field, '') != row.get(field, '') for field in FIELDS):
            errors.append(f'{row["venue_id"]}: registry listing metadata does not match the CSV')
        if entry.get('verified_pin') and not point_valid(entry['verified_pin']):
            errors.append(f'{row["venue_id"]}: invalid stored venue coordinate')
    expected = build_ledger(rows, registry, allow_expired=allow_expired)
    if ledger != expected:
        errors.append('Verification ledger does not match the candidate registry and listings')
    venues = read_json(root / 'venue-coordinates.json', {'venues': {}})['venues']
    overrides = read_json(root / 'venue-coordinate-overrides.json', {'venues': {}})['venues']
    for key, record in expected['records'].items():
        if record['display_precision'] == 'venue':
            rendered = overrides.get(key) or venues.get(key)
            if not rendered or rendered.get('lat') != record['lat'] or rendered.get('lng') != record['lng']:
                errors.append(f'{key}: stored coordinate differs from the evidence-backed pin')
            postcodes_match = lambda left, right: str(left).replace(' ', '').upper() == str(right).replace(' ', '').upper()
            issue = record.get('metadata_issue', {})
            accepted_postcodes = [record['postcode']]
            if issue.get('listed') == record['postcode'] and issue.get('source_url'):
                accepted_postcodes.append(issue.get('reference', ''))
            if not any(any(postcodes_match(e.get('reference_postcode'), pc) for pc in accepted_postcodes) for e in record['evidence']):
                errors.append(f'{key}: coordinate evidence belongs to a different address')
    try:
        if bundle != build_bundle(rows, metadata, postcodes, expected):
            errors.append('Map release does not match candidate listings, evidence and coordinates')
    except ValueError as error:
        errors.append(str(error))
    previous_rows = read_rows(Path(previous_root) / 'pubs.csv') if previous_root else []
    valid, csv_errors, _ = validate_csv(previous_rows, rows)
    errors.extend(csv_errors)
    from validate_coordinate_verification import validate as validate_coordinate_files
    errors.extend(validate_coordinate_files(root, max_evidence_age_days=None if allow_expired else MAX_EVIDENCE_AGE_DAYS))
    if previous_root:
        old = bootstrap_registry(previous_root)
        for row in rows:
            identifier = row['venue_id']
            old_pin = old['venues'].get(identifier, {}).get('verified_pin')
            new_pin = registry['venues'][identifier].get('verified_pin')
            if old_pin and evidence_valid(old_pin):
                if not new_pin or not evidence_valid(new_pin):
                    reviewed = bool(new_pin and new_pin.get('review_required') and new_pin.get('review_reason'))
                    if not allow_review_downgrades or not reviewed:
                        errors.append(f'{identifier}: previously verified pin lost verification')
                elif distance_metres(old_pin, new_pin) > 50:
                    errors.append(f'{identifier}: previously verified pin moved more than 50 metres; review required')
    return errors
