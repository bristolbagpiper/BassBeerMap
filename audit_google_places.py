"""Rotate official Google Places comparisons without importing replacement pins.

Network use requires both GOOGLE_PLACES_ENABLED=true and GOOGLE_PLACES_API_KEY.
No browser scraping, automatic retries, pagination, or raw response persistence.
Request caps cover this job's saved usage, not other use of the Google project;
restrict the key and set project quotas before enabling billing-backed requests.
"""
import argparse
import copy
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from build_place_review_queue import build_queue, digest, non_google
from coordinate_sources import name_matches
from directory_release import active_evidence, distance_metres, point_valid, read_json, write_json


ENDPOINT = 'https://places.googleapis.com/v1/places:searchText'
FIELD_MASK = ('places.id,places.displayName,places.addressComponents,'
              'places.location,places.businessStatus,nextPageToken')
MAX_REQUESTS_PER_RUN = 45
MAX_REQUESTS_PER_DAY = 45
MAX_REQUESTS_PER_MONTH = 1400
MAX_CHECK_AGE_DAYS = 30
CONFIGURATION_ISSUE = 'Google comparison is not configured; no live Google checks ran this time'
COVERAGE_ISSUE = 'Google comparison coverage is incomplete; untested, stale or inconclusive pubs remain'
SAFE_PROVIDER_CODES = frozenset(('response_too_large', 'invalid_response', 'network_unavailable',
                                 'invalid_json', 'invalid_place', 'invalid_address'))


class ProviderError(Exception):
    """Only sanitized error codes may enter state, reports or logs."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None  # Never forward the API key to a redirected host.


def search_places(item, api_key):
    request = Request(ENDPOINT, method='POST', headers={
        'Content-Type': 'application/json', 'X-Goog-Api-Key': api_key,
        'X-Goog-FieldMask': FIELD_MASK,
    }, data=json.dumps(dict(textQuery=item['google_named_search_query'], pageSize=5,
                            languageCode='en', regionCode='GB')).encode())
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(256_001)
        if len(raw) > 256_000:
            raise ProviderError('response_too_large')
        result = json.loads(raw)
        if not isinstance(result, dict) or 'error' in result:
            raise ProviderError('invalid_response')
        return result
    except HTTPError as error:
        raise ProviderError('HTTP_' + str(error.code)) from None
    except (URLError, TimeoutError, OSError):
        raise ProviderError('network_unavailable') from None
    except (ValueError, UnicodeError):
        raise ProviderError('invalid_json') from None


def compact_postcode(value):
    return re.sub(r'\s+', '', str(value)).upper()


def compare(item, entry, result):
    """Consume Google fields in memory; return only operational classifications.

    A nearby marker is corroboration, not a building survey or Bass-stock check.
    Even disagreement only opens a review; it cannot move or approve a pin.
    """
    if not isinstance(result, dict) or not isinstance(result.get('places', []), list):
        raise ProviderError('invalid_response')
    places = result.get('places', [])
    if not places:
        return dict(status='review_needed', reasons=['Google returned no named place; location needs review'])
    if len(places) > 5 or result.get('nextPageToken'):
        return dict(status='review_needed', reasons=['Google results are incomplete or ambiguous; no unique place accepted'])
    pin = entry.get('verified_pin') or {}
    references = [e for e in active_evidence(pin) if non_google(e)]
    names = [item['pub_name']] + [e['reference_name'] for e in references if e.get('reference_name')]
    postcodes = {compact_postcode(item['postcode'])}
    postcodes.update(compact_postcode(e['reference_postcode']) for e in references if e.get('reference_postcode'))
    candidates, ids = [], set()
    for place in places:
        if not isinstance(place, dict):
            raise ProviderError('invalid_place')
        identifier = place.get('id')
        display = place.get('displayName') or {}
        components = place.get('addressComponents', [])
        if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', identifier)
                or identifier in ids or not isinstance(display, dict)
                or not isinstance(display.get('text'), str) or not isinstance(components, list)):
            raise ProviderError('invalid_place')
        ids.add(identifier)
        observed_postcodes = set()
        for component in components:
            if not isinstance(component, dict) or not isinstance(component.get('types', []), list):
                raise ProviderError('invalid_address')
            if 'postal_code' in component.get('types', []):
                observed_postcodes.add(compact_postcode(component.get('longText') or component.get('shortText') or ''))
        if any(name_matches(name, display['text'], item['place_name']) for name in names) and postcodes & observed_postcodes:
            candidates.append(place)
    if len(candidates) != 1:
        return dict(status='review_needed', reasons=['Google did not identify one matching named pub at an expected postcode'])
    place = candidates[0]
    raw_point = place.get('location') or {}
    try:
        lat, lng = raw_point['latitude'], raw_point['longitude']
        if isinstance(lat, bool) or isinstance(lng, bool):
            raise ValueError()
        point = dict(lat=float(lat), lng=float(lng))
        valid = all(math.isfinite(v) for v in point.values()) and point_valid(point)
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    reasons = []
    if not valid:
        reasons.append('Google named-place coordinates are missing or invalid')
    elif item['precision'] != 'venue':
        reasons.append('Google named place found but the published pin remains approximate; precise location needs review')
    elif distance_metres(item['current_pin'], point) > 50:
        reasons.append('Google named-place marker differs by more than 50 metres; existing pin retained for review')
    business_status = place.get('businessStatus')
    if business_status in ('CLOSED_TEMPORARILY', 'CLOSED_PERMANENTLY'):
        reasons.append('Google indicates a possible closure; trading status needs review')
    elif business_status != 'OPERATIONAL':
        reasons.append('Google did not provide a conclusive trading status')
    return dict(status='review_needed' if reasons else 'consistent_within_50m', reasons=reasons,
                place_id=place['id'])


def utc_now():
    return datetime.now(timezone.utc)


def valid_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def bound(check, item):
    return check.get('binding_signature') == item['binding_signature']


def fresh(check, item, now):
    checked = valid_time(check.get('checked_at'))
    return bool(bound(check, item) and checked and timedelta(0) <= now - checked <= timedelta(days=MAX_CHECK_AGE_DAYS))


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    write_json(temporary, state)
    temporary.replace(path)


def load_state(path):
    state = read_json(path, {'schema_version': 1, 'checks': {}, 'usage': {}})
    if (not isinstance(state, dict) or state.get('schema_version') != 1
            or not isinstance(state.get('checks'), dict) or not isinstance(state.get('usage'), dict)):
        raise ValueError('Invalid Google comparison state')
    # Fail closed on corrupt usage rather than resetting billable request counts.
    for key, value in state['usage'].items():
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', key) or not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError('Invalid Google comparison usage counter')
    for check in state['checks'].values():
        if (not isinstance(check, dict) or check.get('status') not in ('review_needed', 'consistent_within_50m', 'unavailable')
                or not isinstance(check.get('reasons'), list)
                or not all(isinstance(reason, str) and reason for reason in check['reasons'])
                or not isinstance(check.get('binding_signature'), str)
                or not valid_time(check.get('attempted_at'))
                or ('checked_at' in check and not valid_time(check['checked_at']))):
            raise ValueError('Invalid saved Google check')
    return state


def audit(root, state_path, limit=45, enabled=False, api_key='', lookup=search_places, now=None):
    if not 0 <= limit <= MAX_REQUESTS_PER_RUN:
        raise ValueError('Google check limit must be between 0 and 45')
    now = now or utc_now()
    if now.tzinfo is None:
        raise ValueError('Google audit time must include timezone')
    root = Path(root)
    bundle = read_json(root / 'directory-release.json')
    registry = read_json(root / 'venue-registry.json')
    queue = build_queue(bundle, registry, read_json(root / 'audit/full-location-audit.json', {}))
    state = copy.deepcopy(load_state(state_path))
    active_ids = {item['venue_id'] for item in queue}
    state['checks'] = {identifier: check for identifier, check in state['checks'].items() if identifier in active_ids}
    # Only counter data and operational decisions persist; Google content does not.
    month = now.strftime('%Y-%m')
    day = now.date().isoformat()
    state['usage'] = {key: value for key, value in state['usage'].items() if key.startswith(month)}
    issues, requests, checked_count, hard_failure = [], 0, 0, False
    configured = bool(enabled and api_key.strip())
    if not configured:
        issues.append(CONFIGURATION_ISSUE)
    else:
        def priority(item):
            previous = state['checks'].get(item['venue_id'], {})
            return (previous.get('attempted_at', '') if bound(previous, item) else '', item['priority'], item['risk_rank'])
        for item in sorted(queue, key=priority)[:limit]:
            used_day = state['usage'].get(day, 0)
            used_month = sum(state['usage'].values())
            if used_day >= MAX_REQUESTS_PER_DAY or used_month >= MAX_REQUESTS_PER_MONTH:
                issues.append('Google comparison request cap reached; remaining checks deferred')
                break
            # Reserve before making a potentially billable request, including errors.
            state['usage'][day] = used_day + 1
            save_state(state_path, state)
            requests += 1
            identifier = item['venue_id']
            prior = state['checks'].get(identifier, {})
            check = copy.deepcopy(prior) if bound(prior, item) else dict(
                status='unavailable', reasons=[], binding_signature=item['binding_signature'])
            check['attempted_at'] = now.isoformat()
            try:
                result = compare(item, registry['venues'][identifier], lookup(item, api_key))
                check = dict(result, binding_signature=item['binding_signature'],
                             checked_at=now.isoformat(), attempted_at=now.isoformat())
                checked_count += 1
            except Exception as error:
                # Never write provider exception text: it may contain key/response data.
                safe_code = isinstance(error, ProviderError) and (str(error) in SAFE_PROVIDER_CODES
                                                               or re.fullmatch(r'HTTP_\d{3}', str(error)))
                code = str(error) if safe_code else 'lookup_failed'
                issues.append('Google Places API unavailable (' + code + '); remaining checks deferred')
                hard_failure = True
            state['checks'][identifier] = check
            save_state(state_path, state)
            if hard_failure:
                break  # Auth/quota/outage must not cause 45 futile billed requests.
    save_state(state_path, state)
    current, findings = 0, []
    for item in queue:
        check = state['checks'].get(item['venue_id'], {})
        current += int(fresh(check, item, now))
        if bound(check, item) and check.get('status') == 'review_needed':
            reasons = list(check['reasons'])
            if not fresh(check, item, now):
                reasons.append('Previous Google finding needs a fresh comparison')
            findings.append(dict(venue_id=item['venue_id'], pub_name=item['pub_name'],
                                 place_name=item['place_name'], postcode=item['postcode'],
                                 binding_signature=item['binding_signature'],
                                 status='review_needed', reason='; '.join(reasons)))
    if current != len(queue):
        issues.append(COVERAGE_ISSUE)
    return dict(schema_version=1, checked_at=now.isoformat(), configured=configured,
                directory_binding_signature=digest(sorted((item['venue_id'], item['binding_signature']) for item in queue)),
                issues=issues, findings=sorted(findings, key=lambda f: f['venue_id']),
                counts=dict(total=len(queue), current=current, unchecked_or_stale=len(queue) - current,
                            checked_this_run=checked_count, requests_this_run=requests, review_count=len(findings)),
                hard_failure=hard_failure)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--state', type=Path, default=Path('google-places-state.json'))
    parser.add_argument('--status', type=Path, default=Path('google-places-status.json'))
    parser.add_argument('--limit', type=int, default=45)
    args = parser.parse_args(argv)
    enabled = os.environ.get('GOOGLE_PLACES_ENABLED', '').lower() == 'true'
    api_key = os.environ.get('GOOGLE_PLACES_API_KEY', '')
    try:
        status = audit(args.root, args.state, args.limit, enabled, api_key)
    except Exception:
        status = dict(schema_version=1, checked_at=utc_now().isoformat(), configured=bool(enabled and api_key.strip()),
                      issues=['Google comparison failed before completing; inspect state and configuration'],
                      counts={}, findings=[], hard_failure=True)
    status.update(run_id=os.environ.get('AUDIT_RUN_ID', ''), run_attempt=int(os.environ.get('AUDIT_RUN_ATTEMPT', '1')),
                  run_url=os.environ.get('AUDIT_RUN_URL', ''))
    write_json(args.status, status)
    if not status['configured']:
        print('::warning::Google comparisons are not configured; no live Google checks passed in this run')
    print(json.dumps(status, indent=2))  # Own listing + operational decisions only.
    return int(status['hard_failure'])


if __name__ == '__main__':
    raise SystemExit(main())
