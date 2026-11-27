"""Look up selected directory identities on source changes using ordinary Maps business pages."""
import json
import os
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode, unquote, urlsplit

from directory_release import distance_metres, point_valid
from hermes_google_browser import _DOM_READ, _point_from_url, _postcode_in_text, GOOGLE_CHALLENGE

IDENTITY = ('pub_name', 'place_name', 'postcode')
MAX_PER_IMPORT = 20


def public_link(row, record):
    if not record or any(record.get(k) != row[k] for k in IDENTITY):
        return None
    if 'place_id' in record and re.fullmatch(r'[A-Za-z0-9_-]{10,256}', record['place_id']):
        return {'place_id': record['place_id']}
    if re.fullmatch(r'https://www\.google\.com/maps\?cid=[1-9]\d{0,19}', record.get('maps_url', '')):
        return {'maps_url': record['maps_url']}
    raise ValueError(f'{row["pub_name"]}: invalid Google business identifier')


def norm(name):
    return re.sub(r'[\W_]+', '', re.sub(r'^the\s+', '', name.casefold().strip()).replace('&', 'and'))


def same_premises(left, right):
    """Allow separate PDF rows to share a listing only with corroborated premises identity."""
    first, second = (entry.get('listing', {}) for entry in (left, right))
    if not first.get('pub_name') or any(first.get(k) != second.get(k) for k in ('pub_name', 'postcode')):
        return False
    a, b = (entry.get('verified_pin') or {} for entry in (left, right))
    if not (point_valid(a) and point_valid(b) and distance_metres(a, b) <= 3):
        return False
    def camra_ids(pin):
        return {e['url'] for e in pin.get('evidence', []) if e.get('source') == 'camra' and
                re.fullmatch(r'https://camra\.org\.uk/pubs/(?:[\w-]*-)?\d+/?', e.get('url', ''))}
    return bool(camra_ids(a) & camra_ids(b))


def business_profile(page):
    category = (page.get('category') or '').strip().lower()
    return (category not in ('building', 'address', 'street', 'road', 'intersection', 'bus stop', 'postal code')
            and bool(category or page.get('businessControls')))


def identify(row, pin, page):
    url = page.get('url', '')
    parsed = urlsplit(url)
    title = page.get('h1') or []
    addresses = page.get('addressButtons') or []
    if (parsed.netloc not in ('www.google.com', 'www.google.co.uk') or parsed.scheme != 'https'
            or not parsed.path.startswith('/maps/place/') or not page.get('namedBusinessPanel') or not business_profile(page)
            or len(title) != 1 or len(addresses) != 1):
        return 'ambiguous', None
    names = {norm(row['pub_name'])}
    if pin.get('alternate_name'):
        names.add(norm(pin['alternate_name']))
    if pin.get('confirmed_google_name'):
        names.add(norm(pin['confirmed_google_name']))
    postcodes = [row['postcode']]
    issue = pin.get('metadata_issue') or {}
    if issue.get('listed') == row['postcode'] and issue.get('source_url'):
        postcodes.append(issue.get('reference', ''))
    address = addresses[0].get('label', '')
    short_name, comma, locality = title[0].partition(',')
    name_ok = norm(title[0]) in names or (comma and norm(short_name) in names and
                                         norm(locality) in norm(row['place_name'] + ' ' + address))
    if not name_ok or not any(_postcode_in_text(pc.replace(' ', ''), address) for pc in postcodes):
        return 'identity_conflict', None
    point = _point_from_url(url)
    if point_valid(pin) and not point:
        return 'ambiguous', None
    if point_valid(pin) and point and distance_metres(pin, point) > 100:
        return 'pin_conflict', None
    tokens = set(re.findall(r'!1s0x[0-9a-fA-F]+:0x([0-9a-fA-F]{1,16})(?=!|/|$)', unquote(parsed.path)))
    if len(tokens) != 1 or not int(next(iter(tokens)), 16):
        return 'ambiguous', None
    cid = int(next(iter(tokens)), 16)
    listing = {**{k: row[k] for k in IDENTITY}, 'maps_url': f'https://www.google.com/maps?cid={cid}'}
    if norm(title[0]) != norm(row['pub_name']):
        listing['matched_name'] = title[0]
    explicit = set(re.findall(r'!(?:1|19)s(ChIJ[A-Za-z0-9_-]{10,250})(?=!|/|$)', unquote(parsed.path)))
    if len(explicit) == 1:
        listing['place_id'] = explicit.pop()
    return 'named_listing', listing


def observe(driver, row, pin, listing=None):
    name = pin.get('alternate_name') or row['pub_name']
    town = row['place_name'].split(':', 1)[0].strip()
    issue = pin.get('metadata_issue') or {}
    postcode = row['postcode']
    if issue.get('listed') == postcode and issue.get('use_reference_for_directions') and issue.get('source_url'):
        postcode = issue.get('reference') or postcode
    query = ', '.join([name, town, postcode, 'United Kingdom'])
    params = {'api': 1, 'query': query, 'hl': 'en'}
    if listing and listing.get('place_id'):
        params['query_place_id'] = listing['place_id']
    driver.get((listing or {}).get('maps_url') or 'https://www.google.com/maps/search/?' + urlencode(params))
    deadline = time.monotonic() + 16
    rejected = followed = False
    while time.monotonic() < deadline:
        page = json.loads(driver.execute_script('return ' + _DOM_READ))
        if '/sorry/' in page['url'] or GOOGLE_CHALLENGE.search(page.get('body', '')):
            return 'unavailable', None
        if page['namedBusinessPanel'] and '/maps/place/' in urlsplit(page['url']).path and _point_from_url(page['url']):
            return identify(row, pin, page)
        if not rejected and urlsplit(page['url']).hostname == 'consent.google.com':
            buttons = driver.find_elements('xpath', "//button[normalize-space(.)='Reject all' or @aria-label='Reject all']")
            button = next((b for b in buttons if b.is_displayed() and b.is_enabled()), None)
            if button:
                button.click()
                rejected = True
        if not listing and page.get('placeLinks') and not followed:
            matches = {link['href'] for link in page['placeLinks'] if norm(link['text']) == norm(name)
                       and urlsplit(link['href']).netloc == 'www.google.com' and '/maps/place/' in link['href']}
            if len(matches) == 1:
                driver.get(matches.pop())
                followed = True
        time.sleep(.5)
    rendered_map = urlsplit(page['url']).path.startswith('/maps') and bool(page.get('body', '').strip())
    return ('ambiguous' if rendered_map else 'unavailable'), None


def targets(rows, registry, previous_rows, today):
    previous = {r['venue_id']: r for r in previous_rows}
    queue = []
    for row in rows:
        key = row['venue_id']
        entry = registry['venues'][key]
        pin = entry.get('verified_pin') or {}
        point = [pin.get('lat'), pin.get('lng')]
        check = entry.get('google_check') or {}
        binding = [row[k] for k in IDENTITY] + point
        changed = any(previous.get(key, {}).get(k) != row[k] for k in IDENTITY)
        rebound = check and check.get('binding') != binding
        unresolved = not public_link(row, entry.get('google_listing'))
        approximate = (pin.get('premises_review') or {}).get('precision') != 'building'
        if not (changed or rebound or unresolved or approximate):
            continue
        last = check.get('date', '')
        if not (changed or rebound) and last and last > (today - timedelta(days=90)).isoformat():
            continue
        priority = 0 if changed or rebound else 1
        queue.append((priority, last, 0 if unresolved else 1, key, row, binding))
    return sorted(queue)[:MAX_PER_IMPORT]


def check_google(rows, registry, previous_rows, today=None, only=None):
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    today = today or date.today()
    queue = targets([r for r in rows if only is None or r['venue_id'] in only], registry, previous_rows, today)
    results = {}
    if not queue:
        return results
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--lang=en-GB')
    options.add_argument('--window-size=1360,920')
    if os.environ.get('CHROME_BIN'):
        options.binary_location = os.environ['CHROME_BIN']
    try:
        with webdriver.Chrome(options=options) as driver:
            driver.set_page_load_timeout(24)
            for _, _, _, key, row, binding in queue:
                entry = registry['venues'][key]
                previous = entry.get('google_listing') or {}
                previous_link = public_link(row, previous)
                pin = entry.get('verified_pin') or {}
                bound_pin = dict(pin, confirmed_google_name=previous.get('matched_name')) if previous_link else pin
                outcome, listing = observe(driver, row, bound_pin, previous_link)
                if previous_link and outcome not in ('named_listing', 'unavailable'):
                    outcome, listing = observe(driver, row, pin)
                    previous_link = None
                    bound_pin = pin
                    if outcome == 'ambiguous':
                        entry.pop('google_listing', None)
                if listing and not (previous_link or {}).get('place_id') and public_link(row, listing) != previous_link:
                    # Maps can identify a building and its tenant separately.
                    # Open the exact visitor URL before saving a newly found ID.
                    candidate = public_link(row, listing)
                    outcome, verified = observe(driver, row, bound_pin, candidate)
                    if not verified or verified['maps_url'] != listing['maps_url']:
                        listing = None
                        if outcome != 'unavailable':
                            outcome = 'destination_conflict'
                results[key] = outcome
                if outcome == 'unavailable':
                    break
                entry['google_check'] = {'binding': binding, 'date': today.isoformat(), 'outcome': outcome}
                if listing:
                    others = (other for vid, other in registry['venues'].items() if vid != key and
                              (other.get('google_listing') or {}).get('maps_url') == listing['maps_url'])
                    if any(not same_premises(entry, other) for other in others):
                        entry['google_check']['outcome'] = results[key] = 'shared_listing'
                    elif not previous_link or not previous_link.get('place_id'):
                        entry['google_listing'] = listing
                elif outcome in ('identity_conflict', 'pin_conflict', 'destination_conflict'):
                    entry.pop('google_listing', None)
    except (WebDriverException, TimeoutError, json.JSONDecodeError) as error:
        results['browser'] = 'unavailable: ' + type(error).__name__
    return results
