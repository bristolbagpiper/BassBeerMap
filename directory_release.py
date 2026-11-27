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
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


def validate_pdf_listings(pdf, rows, metadata=None):
    """Compare every source field and retain the count of repeated rows."""
    from convert_pdf_to_csv import PdfReader, parse_pdf_rows

    def listing_counts(listings):
        return Counter(tuple(row[field] for field in FIELDS) for row in listings)

    highlights = [] if metadata is not None else None
    source = listing_counts(parse_pdf_rows(PdfReader(str(pdf)), highlights))
    if metadata is not None and sorted(highlights) != sorted(metadata.get("new_or_returning", [])):
        return ["New or returning listings disagree with PDF highlighting"]
    published = listing_counts(rows)
    if source == published:
        return []
    return [f'Directory does not match the PDF: {(source - published).total()} missing source rows, '
            f'{(published - source).total()} added or altered rows']


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


def active_evidence(pin):
    """Read current evidence without removing historical records from the pin.

    The current schema marks inactive records with a disposition. Only an
    absent disposition carries current authority; unknown explicit values fail
    closed rather than silently reviving superseded or rejected evidence.
    """
    evidence = pin.get('evidence', []) if isinstance(pin, dict) else []
    if not isinstance(evidence, list):
        return []
    return [item for item in evidence if isinstance(item, dict) and 'disposition' not in item]


def evidence_valid(pin, today=None, allow_expired=False):
    today = today or date.today()
    evidence = active_evidence(pin)
    if not point_valid(pin) or not evidence or pin.get('review_required'):
        return False
    valid = []
    for item in evidence:
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
    errors, changes = [], []
    selected = [{field: str(original.get(field, '')).strip() for field in FIELDS} for original in rows]
    counts = Counter(venue_key(row) for row in selected)
    errors.extend(f'{key}: duplicate PDF identities require review; all source rows must be retained'
                  for key, count in counts.items() if count > 1)
    if errors:
        return selected, registry, {'errors': errors, 'changes': changes}

    # Historical listing corrections, exclusions and duplicate selections are
    # retained as evidence. Only reviewed identity links affect reconciliation.
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
        if decision:
            previous_id = aliases.get(decision.get('previous_key'))
            if (not previous_id or not all(decision.get(f) for f in ('reason', 'source_url', 'checked_at', 'reference_postcode'))
                    or identifier not in (None, previous_id)):
                errors.append(f'{key}: identity alias has incomplete review evidence')
                continue
            identifier = previous_id
            entry = registry['venues'][identifier]
            if key not in entry['aliases']:
                entry['aliases'].append(key)
            reviews = entry.setdefault('identity_reviews', [])
            if decision not in reviews:
                reviews.append(copy.deepcopy(decision))
            # A reviewed correction to listing metadata is not a coordinate move.
            if entry.get('verified_pin') and row['postcode'] != decision['reference_postcode']:
                issue = entry['verified_pin'].setdefault('metadata_issue', {
                    'field': 'postcode', 'listed': row['postcode'],
                    'reference': decision['reference_postcode'], 'source_url': decision['source_url'],
                })
                if (decision.get('use_reference_for_directions') is True and issue['listed'] == row['postcode']
                        and issue['reference'] == decision['reference_postcode']):
                    issue['use_reference_for_directions'] = True
        if identifier is None:
            similar = []
            for old_id, entry in registry['venues'].items():
                old = entry['listing']
                same_place = old.get('place_name', '').casefold() == row['place_name'].casefold()
                same_name = old.get('pub_name', '').casefold() == row['pub_name'].casefold()
                same_postcode = old.get('postcode', '').replace(' ', '').casefold() == row['postcode'].replace(' ', '').casefold()
                # A locality label correction changes the key too. Do not let
                # it replace an absent venue's permanent ID (and reviewed pin)
                # merely because the old locality no longer matches. Matching
                # the full postcode and name flags review, never an auto-merge;
                # a still-active branch can coexist with a genuinely new one.
                possible_identity_change = (same_place and (same_name or same_postcode)
                                            or same_name and same_postcode and bool(row['postcode']))
                if possible_identity_change and entry['current_key'] not in active_keys:
                    similar.append(old_id)
            if similar:
                errors.append(f'{key}: possible rename/locality/postcode change requires identity review ({", ".join(similar)})')
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
    return selected, registry, {'errors': errors, 'changes': changes}


def building_identity_documented(review, sources):
    """Require a cited occupancy check in addition to the building geometry."""
    identity = review.get('identity_sources')
    if not (review.get('building_url') in sources and review.get('geometry_feature')
            and isinstance(identity, list) and identity):
        return False
    geometry_domains = ('openstreetmap.org', 'osm.org', 'osdatahub.os.uk',
                        'minedbuildings.z5.web.core.windows.net', 'overturemaps.org')
    for url in identity:
        if not isinstance(url, str) or url not in sources:
            return False
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        if any(host == domain or host.endswith('.' + domain) for domain in geometry_domains):
            return False
        if url == review['building_url']:
            checks = review.get('external_premises_checks')
            outlines = checks.get('council_premises_outline') if isinstance(checks, dict) else None
            if not isinstance(outlines, list) or url not in outlines:
                return False
        if host.endswith('.arcgisonline.com') and '/world_imagery/' in parsed.path.lower():
            return False
        if host in ('maps.app.goo.gl', 'goo.gl'):
            return False
        if (host.startswith('maps.google.') or
                (host in ('google.co.uk', 'www.google.co.uk', 'google.com', 'www.google.com')
                 and parsed.path.startswith('/maps'))):
            query = parse_qs(parsed.query)
            if query.get('map_action') != ['pano'] or not query.get('pano'):
                return False
    return True


def premises_review_valid(row, pin):
    review = pin.get('premises_review')
    if not isinstance(review, dict):
        return False
    try:
        dated = date.fromisoformat(review['checked_at']) <= date.today()
    except (KeyError, ValueError, TypeError):
        return False
    sources = review.get('sources')
    return (dated and review.get('venue_key') == venue_key(row)
            and review.get('point') == {k: pin.get(k) for k in ('lat', 'lng')}
            and review.get('precision') in ('building', 'site', 'reference')
            and isinstance(review.get('basis'), str) and bool(review['basis'].strip())
            and isinstance(sources, list) and bool(sources)
            and all(isinstance(url, str) and re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:[/?#][^\s]*)?', url) for url in sources)
            and (review['precision'] != 'building' or building_identity_documented(review, sources)))


def reviewed_trading_name(row, entry):
    for review in entry.get('listing_reviews', []):
        if review.get('kind') == 'correction' and review.get('original_key') == venue_key(row):
            name = review.get('decision', {}).get('changes', {}).get('pub_name')
            if name and name != row['pub_name']:
                return name
    return None


def build_ledger(rows, registry, allow_expired=False):
    records = {}
    for row in rows:
        entry = registry['venues'][row['venue_id']]
        pin = entry.get('verified_pin')
        # Ageing a source does not move a physical building to its postcode.
        verified = bool(pin and evidence_valid(pin, allow_expired=True))
        record = {
            'venue_id': row['venue_id'], 'pub_name': row['pub_name'], 'place_name': row['place_name'], 'postcode': row['postcode'],
            'status': 'verified_venue' if verified else 'venue_coordinate_requires_review' if pin else 'postcode_approximate',
            'display_precision': 'venue' if verified else 'postcode',
            'premises_precision': 'reference' if verified else 'postcode',
            'coordinate_source': pin.get('source', '') if pin else 'postcode',
            'evidence': copy.deepcopy(pin.get('evidence', [])) if pin else [],
        }
        if pin:
            record.update(lat=pin['lat'], lng=pin['lng'])
            review = pin.get('premises_review', {})
            if verified and premises_review_valid(row, pin):
                record['premises_precision'] = review['precision']
                if review.get('visitor_hint'):
                    record['visitor_hint'] = review['visitor_hint']
            if pin.get('metadata_issue'):
                record['metadata_issue'] = dict(pin['metadata_issue'], listed=row['postcode'])
        alternate_name = reviewed_trading_name(row, entry)
        if alternate_name:
            record['alternate_name'] = alternate_name
        records[venue_key(row)] = record
    evidence_dates = [e['checked_at'] for r in records.values() for e in active_evidence(r) if e.get('checked_at')]
    return {
        'schema_version': 2, 'generated_at': max(evidence_dates, default=''),
        'policy': {'max_evidence_age_days': MAX_EVIDENCE_AGE_DAYS, 'venue_pin': 'Building, site and reference positions have separate precision labels. Missing positions use the listed postcode.'},
        'counts': dict(Counter(r['status'] for r in records.values())),
        'premises_counts': dict(Counter(r['premises_precision'] for r in records.values())), 'records': records,
    }


def directions_address(record):
    """Choose a street address supported by the evidence for this venue pin."""
    issue = record.get('metadata_issue') or {}
    postcode = record['postcode']
    if issue:
        if (issue.get('use_reference_for_directions') is not True or issue.get('listed') != postcode
                or not issue.get('source_url') or not issue.get('reference')):
            return None
        postcode = issue['reference']
    compact_postcode = ''.join(postcode.upper().split())
    candidates = []
    for item in active_evidence(record):
        street = item.get('reference_address')
        if not isinstance(street, str):
            continue
        street = ' '.join(street.split())
        reference_postcode = ''.join(str(item.get('reference_postcode') or '').upper().split())
        if (street and reference_postcode == compact_postcode
                and evidence_valid(dict(record, evidence=[item]), allow_expired=True)):
            candidates.append({'street': street, 'postcode': postcode, 'checked_at': item['checked_at']})
    # The browser checks the address date separately from the pin's date.
    return max(candidates, key=lambda address: address['checked_at'], default=None)


def build_bundle(rows, metadata, postcodes, ledger, notices=None, registry=None):
    from google_listings import public_link, same_premises
    locations = {}
    business_owners = {}
    highlighted = set(metadata.get('new_or_returning', []))
    for row in rows:
        record = ledger['records'][venue_key(row)]
        precision = record['display_precision']
        point = record if precision == 'venue' else postcodes.get(row['postcode'])
        if not point_valid(point or {}):
            raise ValueError(f'{venue_key(row)}: no usable {precision} location')
        locations[row['venue_id']] = {'lat': point['lat'], 'lng': point['lng'], 'precision': precision}
        if registry:
            listing = registry['venues'][row['venue_id']].get('google_listing')
            link = public_link(row, listing)
            if link:
                identifier = listing.get('maps_url') or link['place_id']
                business_owners.setdefault(identifier, []).append(row['venue_id'])
                locations[row['venue_id']]['google_listing'] = link
        if venue_key(row) in highlighted:
            locations[row['venue_id']]['new_or_returning'] = True
        fallback = postcodes.get(row['postcode'])
        if not point_valid(fallback or {}):
            raise ValueError(f'{venue_key(row)}: postcode fallback is invalid or unavailable')
        locations[row['venue_id']]['postcode_location'] = fallback
        if precision == 'venue':
            locations[row['venue_id']]['premises_precision'] = record.get('premises_precision', 'reference')
            if record.get('visitor_hint'):
                locations[row['venue_id']]['visitor_hint'] = record['visitor_hint']
            locations[row['venue_id']]['checked_at'] = max(e['checked_at'] for e in active_evidence(record))
            address = directions_address(record)
            if address:
                locations[row['venue_id']]['directions_address'] = address
        if record.get('metadata_issue'):
            locations[row['venue_id']]['metadata_issue'] = record['metadata_issue']
        if record.get('alternate_name'):
            locations[row['venue_id']]['alternate_name'] = record['alternate_name']
    for owners in business_owners.values():
        if any(not same_premises(registry['venues'][owners[0]], registry['venues'][key]) for key in owners[1:]):
            for key in owners:
                locations[key].pop('google_listing', None)
    payload = {'schema_version': 1, 'metadata': metadata, 'rows': rows, 'locations': locations,
               'max_evidence_age_days': MAX_EVIDENCE_AGE_DAYS}
    if notices is not None:
        if notices.get('schema_version') != 1 or not isinstance(notices.get('notices'), list) or not isinstance(notices.get('manual_hides'), list):
            raise ValueError('Invalid curated trading notice manifest')
        hidden = set(notices['manual_hides'])
        visible = {}
        seen = set()
        for notice in notices['notices']:
            identifier = notice['venue_id']
            if identifier in seen or not isinstance(notice.get('text'), str) or not notice['text'].strip():
                raise ValueError(f'Invalid or duplicate trading notice: {identifier}')
            for field in ('published_at', 'review_by'):
                if field == 'review_by' and field not in notice:
                    continue
                value = notice.get(field)
                try:
                    valid_date = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
                except ValueError:
                    valid_date = False
                if not valid_date:
                    raise ValueError(f'Invalid trading notice {field}: {identifier}')
            if 'review_by' in notice and notice['review_by'] < notice['published_at']:
                raise ValueError(f'Trading notice review precedes publication: {identifier}')
            seen.add(identifier)
            if identifier in locations and identifier not in hidden:
                visible[identifier] = notice
        payload['trading_notices'] = visible
    payload['release_id'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return payload


def write_release(root, rows, registry, metadata, postcodes, *, source_root=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    source_notices = Path(source_root or root) / 'trading-notices.json'
    notices = read_json(source_notices)
    if source_root and source_notices.exists() and source_notices.resolve() != (root / source_notices.name).resolve():
        (root / source_notices.name).write_bytes(source_notices.read_bytes())
    from apply_closed_reviews import apply_decisions
    decisions = read_json(Path(source_root or root) / 'audit/closed-review-decisions.json', {})
    apply_decisions(registry, decisions)
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
    write_json(root / 'directory-release.json', build_bundle(rows, metadata, postcodes, ledger, notices, registry))
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
    for row in rows:
        pin = registry['venues'][row['venue_id']].get('verified_pin') or {}
        review = pin.get('premises_review')
        if review is not None and (not isinstance(review, dict) or
                (review.get('venue_key') == venue_key(row) and not premises_review_valid(row, pin))):
            errors.append(f"{row['venue_id']}: incomplete or stale evidence for reviewed premises")
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
            if not any(any(postcodes_match(e.get('reference_postcode'), pc) for pc in accepted_postcodes) for e in active_evidence(record)):
                errors.append(f'{key}: coordinate evidence belongs to a different address')
    try:
        if bundle != build_bundle(rows, metadata, postcodes, expected, read_json(root / 'trading-notices.json'), registry):
            errors.append('Map release does not match candidate listings, evidence and coordinates')
    except ValueError as error:
        errors.append(str(error))
    previous_rows = read_rows(Path(previous_root) / 'pubs.csv') if previous_root else []
    valid, csv_errors, _ = validate_csv(previous_rows, rows)
    errors.extend(csv_errors)
    from validate_coordinate_verification import validate as validate_coordinate_files
    errors.extend(validate_coordinate_files(root))
    if previous_root:
        old = bootstrap_registry(previous_root)
        previously_published = {row.get('venue_id') for row in previous_rows}
        for row in rows:
            identifier = row['venue_id']
            old_pin = old['venues'].get(identifier, {}).get('verified_pin')
            new_pin = registry['venues'][identifier].get('verified_pin')
            if old_pin and evidence_valid(old_pin, allow_expired=True):
                if not new_pin or not evidence_valid(new_pin, allow_expired=True):
                    reviewed = bool(new_pin and new_pin.get('review_required') and new_pin.get('review_reason'))
                    may_downgrade = allow_review_downgrades or identifier not in previously_published
                    if not may_downgrade or not reviewed:
                        errors.append(f'{identifier}: previously verified pin lost verification')
                elif (distance_metres(old_pin, new_pin) > 50 or
                      (old_pin.get('premises_review') and any(old_pin[k] != new_pin[k] for k in ('lat', 'lng')))):
                    review = new_pin.get('premises_review') or {}
                    previous = {k: old_pin[k] for k in ('lat', 'lng')}
                    latest = (registry['venues'][identifier].get('location_approvals') or [{}])[-1]
                    explicit = (latest.get('previous_pin') == old_pin and latest.get('venue_key') == venue_key(row)
                                and latest.get('approved_point') == {k: new_pin[k] for k in ('lat', 'lng')}
                                and bool(latest.get('reviewed_by') and latest.get('comment_id'))
                                and str(latest.get('review_url', '')).startswith('https://github.com/')
                                and any(e.get('url') == latest.get('source_url') for e in active_evidence(new_pin)))
                    if not explicit and not (premises_review_valid(row, new_pin) and review.get('precision') in ('building', 'site')
                            and review.get('previous_point') == previous and len({url.split('/')[2] for url in review['sources']}) >= 2):
                        errors.append(f'{identifier}: previously verified pin moved more than 50 metres or changed a premises-reviewed point; review required')
    return errors
