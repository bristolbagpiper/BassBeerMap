"""Read factual venue identity/address/coordinate evidence from public sources."""
import html
import json
import re
from datetime import date
from urllib.request import Request, urlopen

from directory_release import distance_metres, point_valid
from resolve_venue_coordinates_from_fhrs import choose_match, search


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
    return dict(point, name=entity['name'], postcode=entity.get('address', {}).get('postalCode', ''),
                address=entity.get('address', {}).get('streetAddress', ''))


def name_matches(listing, reference):
    # CAMRA appends town and SEO text to the JSON-LD name. Require the full
    # distinctive listing name at the start; do not accept substring matches.
    normal = lambda value: re.sub(r'[^a-z0-9]', '', re.sub(r'\band\b|&', '', re.sub(r'^the\s+', '', value.casefold())))
    wanted = normal(re.sub(r'\s*\(PMC\)', '', listing, flags=re.I))
    actual = normal(reference.split(',')[0].split(' - ')[0])
    if wanted == actual:
        return True
    return any(actual == wanted + suffix or wanted == actual + suffix
               for suffix in ('inn', 'hotel', 'pub', 'bar', 'alehouse', 'publichouse'))


def recheck_pin(row, pin):
    """A reachable page alone never refreshes the verification date."""
    evidence = pin.get('evidence', [])
    item = next((e for e in evidence if e.get('url', '').startswith('https://camra.org.uk/pubs/')), None)
    if item:
        observed = parse_camra(fetch_text(item['url']))
        if not name_matches(row['pub_name'], observed['name']):
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
        observed_pin = resolve_new_pin(row)
        if observed_pin and distance_metres(pin, observed_pin) <= 50:
            return {'status': 'confirmed', 'evidence': observed_pin['evidence'][0]}
        return {'status': 'conflict', 'reason': 'FSA no longer provides an unambiguous agreeing venue', 'source_url': fsa_item['url']}
    return {'status': 'manual_review', 'reason': 'Manual map evidence needs a fresh human address/map check'}


def resolve_new_pin(row):
    # Query all businesses at the full postcode so a name-filtered response
    # cannot conceal another same-name venue at a different point.
    candidates = search(row, include_name=False)
    match = choose_match(row, candidates)
    if not match:
        return None
    identifier = match.get('fhrs_id')
    if not identifier:
        return None
    # Official food-business geocodes can be postcode centres. Require a
    # separately mapped named CAMRA venue before displaying a new precise pin.
    reference = camra_reference(row, match)
    if not reference:
        return None
    return {'lat': reference['lat'], 'lng': reference['lng'], 'source': 'camra-and-fhrs', 'evidence': [{
        'source': 'food-standards-agency-fhrs',
        'url': f'https://ratings.food.gov.uk/business/{identifier}',
        'checked_at': date.today().isoformat(), 'match': 'unique_name_and_full_postcode',
        'reference_coordinate': {'lat': match['lat'], 'lng': match['lng']},
        'reference_postcode': row['postcode'],
    }, {
        'source': 'camra', 'url': reference['url'], 'checked_at': date.today().isoformat(),
        'match': 'named_venue_full_postcode_and_independent_fsa_agreement',
        'reference_coordinate': {'lat': reference['lat'], 'lng': reference['lng']},
        'reference_postcode': reference['postcode'], 'reference_name': reference['name'],
    }]}


def camra_reference(row, point):
    from audit.fetch_camra import fetch
    candidates = fetch((point['lat'] - 0.02, point['lat'] + 0.02, point['lng'] - 0.02, point['lng'] + 0.02))['venues']
    compact = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    matches = []
    for candidate in candidates:
        if candidate.get('PremisesStatus') == 'X' or not name_matches(row['pub_name'], candidate.get('Name', '')):
            continue
        if compact(candidate.get('Postcode')) != compact(row['postcode']):
            continue
        try:
            location = {'lat': float(candidate['Latitude']), 'lng': float(candidate['Longitude'])}
            if point_valid(location):
                matches.append(dict(location, name=candidate['Name'], postcode=candidate['Postcode'],
                                    url=f"https://camra.org.uk/pubs/{candidate['IncID']}"))
        except (KeyError, TypeError, ValueError):
            continue
    if not matches or any(distance_metres(matches[0], other) > 50 for other in matches):
        return None
    return matches[0] if distance_metres(point, matches[0]) <= 50 else None
