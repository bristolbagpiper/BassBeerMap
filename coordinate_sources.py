"""Read factual venue identity/address/coordinate evidence from public sources."""
import html
import json
import re
from datetime import date
from functools import lru_cache
from urllib.request import Request, urlopen

from directory_release import distance_metres, point_valid
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
    return dict(point, name=entity['name'], postcode=entity.get('address', {}).get('postalCode', ''),
                address=entity.get('address', {}).get('streetAddress', ''))


@lru_cache(maxsize=300)
def camra_page(url):
    # The map API can omit street/postcode fields. Read the named venue's
    # structured address instead of treating that omission as no address match.
    return parse_camra(fetch_text(url))


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
        reviewed_name = item.get('reference_name', '')
        if not name_matches(row['pub_name'], observed['name']) and observed['name'] != reviewed_name:
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


def resolve_new_pin(row, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(checked_at=date.today().isoformat(), reason='FSA lookup failed',
                       sources={'fsa': {'status': 'unavailable'}, 'camra': {'status': 'not_checked'}})
    # Query all businesses at the full postcode so a name-filtered response
    # cannot conceal another same-name venue at a different point.
    candidates = search(row, include_name=False)
    match = choose_match(row, candidates)
    if not match:
        compact = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
        addressed = [c for c in candidates if compact(c.get('PostCode')) == compact(row['postcode'])]
        named = [c for c in addressed if names_match(row['pub_name'], str(c.get('BusinessName') or ''))]
        status, reason = ('no_postcode_match', 'FSA returned no businesses at the listed full postcode') if not addressed else (
            ('no_name_match', 'FSA businesses at the postcode do not match the pub name') if not named else (
                ('missing_or_ambiguous_coordinates', 'FSA matching business coordinates are missing or disagree')))
        diagnostics['sources']['fsa'] = {'status': status, 'records_returned': len(candidates),
                                          'postcode_matches': len(addressed), 'name_matches': len(named)}
        diagnostics['reason'] = reason + '; CAMRA not checked'
        return None
    diagnostics['sources']['fsa'] = {'status': 'matched', 'lat': match['lat'], 'lng': match['lng']}
    identifier = match.get('fhrs_id')
    if not identifier:
        diagnostics['reason'] = 'FSA match has no business identifier; CAMRA not checked'
        return None
    diagnostics['sources']['fsa']['url'] = f'https://ratings.food.gov.uk/business/{identifier}'
    # Official food-business geocodes can be postcode centres. Require a
    # separately mapped named CAMRA venue before displaying a new precise pin.
    diagnostics['sources']['camra'] = {'status': 'unavailable'}
    diagnostics['reason'] = 'CAMRA lookup failed'
    reference = camra_reference(row, match, diagnostics)
    if not reference:
        if diagnostics['sources']['camra']['status'] == 'unavailable':
            diagnostics['sources']['camra']['status'] = 'unmatched'
            diagnostics['reason'] = 'No agreeing CAMRA venue match'
        return None
    diagnostics['sources']['camra'] = dict(status='matched', **reference)
    diagnostics['reason'] = 'FSA and CAMRA agree on named venue, full postcode and location'
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


def camra_reference(row, point, diagnostics=None):
    if diagnostics is not None:
        diagnostics.setdefault('sources', {})
    from audit.fetch_camra import fetch
    candidates = fetch((point['lat'] - 0.02, point['lat'] + 0.02, point['lng'] - 0.02, point['lng'] + 0.02))['venues']
    compact = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    matches = []
    for candidate in candidates:
        if candidate.get('PremisesStatus') == 'X' or not name_matches(row['pub_name'], candidate.get('Name', '')):
            continue
        url = f"https://camra.org.uk/pubs/{candidate['IncID']}"
        if not candidate.get('Postcode'):
            observed = camra_page(url)
            if not name_matches(row['pub_name'], observed['name']):
                continue
            candidate = dict(candidate, Name=observed['name'], Postcode=observed['postcode'],
                             Street=observed['address'], Latitude=observed['lat'], Longitude=observed['lng'])
        if compact(candidate.get('Postcode')) != compact(row['postcode']):
            continue
        try:
            location = {'lat': float(candidate['Latitude']), 'lng': float(candidate['Longitude'])}
            if point_valid(location):
                matches.append(dict(location, name=candidate['Name'], postcode=candidate['Postcode'],
                                    address=candidate.get('Street') or '', url=url))
        except (KeyError, TypeError, ValueError):
            continue
    if not matches:
        if diagnostics is not None:
            diagnostics['sources']['camra'] = {'status': 'unmatched', 'records_returned': len(candidates)}
            diagnostics['reason'] = 'CAMRA search returned no same-name/full-postcode venue'
        return None
    if any(distance_metres(matches[0], other) > 50 for other in matches):
        if diagnostics is not None:
            diagnostics['sources']['camra'] = {'status': 'ambiguous', 'matches': matches}
            diagnostics['reason'] = 'CAMRA has multiple matching venues at different locations'
        return None
    difference = round(distance_metres(point, matches[0]), 1)
    if difference > 50:
        if diagnostics is not None:
            diagnostics['sources']['camra'] = dict(status='coordinate_disagreement', difference_metres=difference, **matches[0])
            diagnostics['reason'] = f'CAMRA venue found, but its coordinate is {difference} metres from FSA (maximum 50)'
        return None
    return matches[0]
