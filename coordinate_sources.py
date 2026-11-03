"""Read factual venue identity/address/coordinate evidence from public sources."""
import html
import json
import re
from datetime import date
from functools import lru_cache
from urllib.parse import quote
from urllib.request import Request, urlopen

from directory_release import active_evidence, distance_metres, point_valid
from owner_location_review import source_identity_title, source_name_acknowledged
from resolve_venue_coordinates_from_fhrs import choose_match, names_match, search


def fetch_text(url):
    request = Request(url, headers={'User-Agent': 'BassBeerMap coordinate verification (github.com/bristolbagpiper/BassBeerMap)'})
    with urlopen(request, timeout=20) as response:
        return response.read(4_000_000).decode('utf-8', errors='replace')


def parse_camra(text):
    documents = []
    for raw in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(html.unescape(raw))
            documents.extend(data if isinstance(data, list) else data.get('@graph', [data]))
        except (ValueError, TypeError):
            continue
    venues = [item for item in documents if item.get('@type') in ('BarOrPub', 'Restaurant', 'LocalBusiness') and item.get('geo')]
    if len(venues) != 1:
        raise ValueError('Source does not contain one named venue with coordinate evidence')
    entity = venues[0]
    point = {'lat': float(entity['geo']['latitude']), 'lng': float(entity['geo']['longitude'])}
    if not point_valid(point):
        raise ValueError('Source coordinate is invalid or outside the directory region')
    address = entity.get('address') or {}
    return dict(point, name=entity['name'], postcode=address.get('postalCode', ''),
                address=address.get('streetAddress', ''), locality=address.get('addressLocality', ''))


@lru_cache(maxsize=300)
def camra_page(url):
    # The map API can omit street/postcode fields. Read the named venue's
    # structured address instead of treating that omission as no address match.
    return parse_camra(fetch_text(url))


def name_matches(listing, reference, place_name=''):
    # CAMRA appends town and SEO text to the JSON-LD name. Require the full
    # distinctive listing name at the start; do not accept substring matches.
    def normal(value):
        value = re.sub(r'\brugby\s+football\s+club\b', 'rugby club', value.casefold())
        return re.sub(r'[^a-z0-9]', '', re.sub(r'\band\b|&', '', re.sub(r'^(?:the|ye)\s+', '', value)))
    wanted = normal(re.sub(r'\s*\(PMC\)', '', listing, flags=re.I))
    title = reference.split(',')[0].split(' - ')[0].strip()
    # CAMRA may append an explicit alias to the primary title. The alias is
    # never itself a match; other parenthetical qualifiers retain their force.
    title = re.sub(r'\s+\(also known as [^()]+\)$', '', title, flags=re.I)
    actual = normal(title)
    if (wanted == actual or (place_name and normal(f'{listing} {place_name}') == actual)
            or (place_name and normal(f'{listing} at {place_name}') == actual)):
        return True
    if place_name:
        # Some CAMRA titles prefix the venue name with the town. Accept that
        # format only when the prefix is the first locality in the listing.
        locality_prefixes = (part.strip() for part in place_name.split(':'))
        if any(normal(prefix) and actual == normal(prefix) + wanted
               for prefix in locality_prefixes):
            return True
    return any(actual == wanted + suffix or wanted == actual + suffix
               for suffix in ('inn', 'hotel', 'pub', 'bar', 'alehouse', 'publichouse'))


def recheck_pin(row, pin):
    """A reachable page alone never refreshes the verification date."""
    evidence = active_evidence(pin)
    def priority(item):
        try:
            checked = date.fromisoformat(item.get('checked_at', ''))
            if checked > date.today():
                checked = date.min
        except (TypeError, ValueError):
            checked = date.min
        return (checked, bool(item.get('reference_name')), bool(item.get('reviewed_by')),
                bool(item.get('reference_address')), bool(item.get('reference_coordinate')))
    item = max((e for e in evidence if e.get('url', '').startswith('https://camra.org.uk/pubs/')),
               key=priority, default=None)
    if item:
        observed = parse_camra(fetch_text(item['url']))
        reviewed_name = item.get('reference_name', '')
        reviewed_match = bool(reviewed_name and source_identity_title(reviewed_name) == source_identity_title(observed['name']))
        acknowledged = source_name_acknowledged(row, pin, item['url'], observed)
        if not name_matches(row['pub_name'], observed['name'], row.get('place_name', '')) and not reviewed_match and not acknowledged:
            return {'status': 'conflict', 'reason': 'Reference venue name changed', 'source_url': item['url']}
        expected_postcode = item.get('reference_postcode') or pin.get('metadata_issue', {}).get('reference') or row['postcode']
        compact = lambda value: re.sub(r'\s+', '', value).upper()
        if compact(observed['postcode']) != compact(expected_postcode):
            return {'status': 'conflict', 'reason': 'Reference address/postcode changed', 'source_url': item['url']}
        if distance_metres(pin, observed) > 50:
            return {'status': 'conflict', 'reason': 'Reference coordinate changed by more than 50 metres', 'source_url': item['url']}
        updated = dict(item, checked_at=date.today().isoformat(), reference_coordinate={'lat': observed['lat'], 'lng': observed['lng']},
                       reference_postcode=observed['postcode'], reference_name=observed['name'], reference_address=observed['address'])
        return {'status': 'confirmed', 'evidence': updated}
    fsa_item = next((e for e in evidence if e.get('source') in ('food_standards_agency', 'food-standards-agency-fhrs')), None)
    if fsa_item:
        diagnostics = {}
        observed_pin = resolve_new_pin(row, diagnostics, centre=pin)
        if observed_pin and distance_metres(pin, observed_pin) <= 50:
            return {'status': 'confirmed', 'evidence': observed_pin['evidence'][0]}
        if observed_pin:
            return {'status': 'conflict', 'reason': 'CAMRA reference differs from the saved coordinate by more than 50 metres', 'source_url': observed_pin['evidence'][0]['url']}
        # Failure to find supporting data is not proof that an accepted pin is
        # wrong. Keep it while exposing the primary-source check limitation.
        return {'status': 'unavailable', 'reason': diagnostics.get('reason', 'CAMRA reference could not be established'), 'source_url': fsa_item['url']}
    return {'status': 'manual_review', 'reason': 'Manual map evidence needs a fresh human address/map check'}


@lru_cache(maxsize=500)
def postcode_centre(postcode):
    payload = json.loads(fetch_text('https://api.postcodes.io/postcodes/' + quote(postcode, safe='')))
    result = payload.get('result') or {}
    point = {'lat': result.get('latitude'), 'lng': result.get('longitude')}
    if not point_valid(point):
        raise ValueError('No usable postcode search area')
    return point


def fsa_support(row, diagnostics):
    """Record a secondary comparison; it can never approve or veto a pin."""
    try:
        candidates = search(row, include_name=False)
        match = choose_match(row, candidates)
    except Exception as error:
        diagnostics['sources']['fsa'] = {'status': 'unavailable', 'error': type(error).__name__}
        diagnostics.setdefault('source_warnings', []).append('Supporting FSA lookup unavailable: ' + type(error).__name__)
        return None
    if match and not point_valid(match):
        diagnostics['sources']['fsa'] = {'status': 'invalid_coordinates'}
        return None
    if match:
        check = dict(status='matched', lat=match['lat'], lng=match['lng'])
        if match.get('fhrs_id'):
            check['url'] = f"https://ratings.food.gov.uk/business/{match['fhrs_id']}"
        diagnostics['sources']['fsa'] = check
        return match
    compact = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    addressed = [c for c in candidates if compact(c.get('PostCode')) == compact(row['postcode'])]
    named = [c for c in addressed if names_match(row['pub_name'], str(c.get('BusinessName') or ''))]
    status = ('no_postcode_match' if not addressed else 'no_name_match' if not named
              else 'missing_or_ambiguous_coordinates')
    diagnostics['sources']['fsa'] = dict(status=status, records_returned=len(candidates),
                                          postcode_matches=len(addressed), name_matches=len(named))
    return None


def resolve_new_pin(row, diagnostics=None, *, centre=None):
    """Approve an unambiguous named CAMRA address; FSA only adds context.

    A supplied centre is a postcode search area, never coordinate evidence.
    Accepted/owner-reviewed pins are retained by callers, not moved here.
    """
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(checked_at=date.today().isoformat(), reason='CAMRA lookup unavailable',
                       sources={'camra': {'status': 'not_checked'}, 'fsa': {'status': 'not_checked'}},
                       source_warnings=[])
    reference = None
    try:
        centre = centre if point_valid(centre) else postcode_centre(row['postcode'])
        reference = camra_reference(row, centre, diagnostics)
    except Exception as error:
        diagnostics['sources']['camra'] = dict(status='unavailable', error=type(error).__name__)
        diagnostics['reason'] = 'Primary CAMRA lookup unavailable: ' + type(error).__name__
    # Run independently even when CAMRA was inconclusive. The result supplies
    # diagnostics and agreement evidence, never a discovery prerequisite.
    match = fsa_support(row, diagnostics)
    if not reference:
        return None
    diagnostics['sources']['camra'] = dict(status='matched', **reference)
    diagnostics.update(status='confirmed', reason='CAMRA identifies one named venue, full postcode and mapped address; FSA is supporting information only')
    evidence = [{
        'source': 'camra', 'url': reference['url'], 'checked_at': diagnostics['checked_at'],
        'match': 'primary_named_venue_full_postcode_and_address',
        'reference_coordinate': {'lat': reference['lat'], 'lng': reference['lng']},
        'reference_postcode': reference['postcode'], 'reference_name': reference['name'],
        'reference_address': reference.get('address', ''), 'reference_locality': reference.get('locality', ''),
    }]
    if match:
        difference = round(distance_metres(reference, match), 1)
        diagnostics['sources']['fsa']['difference_metres'] = difference
        diagnostics['sources']['fsa']['status'] = 'coordinate_agreement' if difference <= 50 else 'coordinate_disagreement'
        if difference <= 50 and match.get('fhrs_id'):
            evidence.append(dict(source='food-standards-agency-fhrs',
                url=f"https://ratings.food.gov.uk/business/{match['fhrs_id']}", checked_at=diagnostics['checked_at'],
                match='supporting_name_and_full_postcode_agreement',
                reference_coordinate={'lat': match['lat'], 'lng': match['lng']}, reference_postcode=row['postcode']))
    return dict(lat=reference['lat'], lng=reference['lng'], source='camra-primary', evidence=evidence,
                additional_checks={'fsa': dict(diagnostics['sources']['fsa'])})


def camra_reference(row, point, diagnostics=None):
    """Discover independently of FSA and inspect the named CAMRA page.

    Split a capped map response once; if any area is still incomplete, retain
    candidates for review rather than declaring an unambiguous match.
    """
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.setdefault('sources', {})
    from audit.fetch_camra import fetch

    def read_area(bounds):
        result = fetch(bounds)
        south, north, west, east = bounds
        inside = []
        for candidate in result['venues']:
            try:
                lat, lng = float(candidate['Latitude']), float(candidate['Longitude'])
                if south <= lat <= north and west <= lng <= east:
                    inside.append(candidate)
            except (KeyError, TypeError, ValueError):
                continue
        return inside, result.get('total', len(inside)) <= len(inside)

    south, north = point['lat'] - .02, point['lat'] + .02
    west, east = point['lng'] - .02, point['lng'] + .02
    candidates, complete = read_area((south, north, west, east))
    if not complete:
        areas = [(south, point['lat'], west, point['lng']), (south, point['lat'], point['lng'], east),
                 (point['lat'], north, west, point['lng']), (point['lat'], north, point['lng'], east)]
        partitions = [read_area(bounds) for bounds in areas]
        candidates = list({str(c['IncID']): c for records, _ in partitions for c in records}.values())
        complete = all(done for _, done in partitions)
    compact = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    matches, conflicts = [], []
    for candidate in candidates:
        if candidate.get('PremisesStatus') == 'X' or not name_matches(row['pub_name'], candidate.get('Name', ''), row.get('place_name', '')):
            continue
        url = f"https://camra.org.uk/pubs/{candidate['IncID']}"
        observed = camra_page(url)
        if not name_matches(row['pub_name'], observed['name'], row.get('place_name', '')):
            continue
        if not point_valid(observed) or distance_metres(point, observed) > 6000:
            continue
        reference = dict(observed, url=url)
        if compact(observed.get('postcode')) != compact(row['postcode']):
            conflicts.append(reference)
            continue
        locality = str(observed.get('locality', '')).strip().casefold()
        if not str(observed.get('address', '')).strip() and not (locality and locality == str(row.get('place_name', '')).strip().casefold()):
            continue
        matches.append(reference)
    diagnostics['sources']['camra'] = dict(status='unmatched', records_returned=len(candidates),
        coverage_complete=complete, candidates=matches + conflicts)
    if not complete:
        diagnostics['sources']['camra']['status'] = 'incomplete'
        diagnostics['reason'] = 'CAMRA discovery coverage is incomplete; candidates require review'
        return None
    if not matches:
        diagnostics['sources']['camra']['status'] = 'postcode_conflict' if conflicts else 'unmatched'
        diagnostics['reason'] = ('CAMRA address/postcode conflict: named address has a different full postcode; address review required' if conflicts
                                 else 'CAMRA search returned no unambiguous named address at the full postcode')
        return None
    if len(matches) > 1:
        diagnostics['sources']['camra']['status'] = 'ambiguous'
        diagnostics['reason'] = 'CAMRA has multiple named venues at the same postcode; identity review required'
        return None
    diagnostics['sources']['camra'] = dict(status='matched', coverage_complete=True, **matches[0])
    return matches[0]

