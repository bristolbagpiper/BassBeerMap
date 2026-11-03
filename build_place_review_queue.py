"""Offline, private Google place-check worklist; never promotes a public pin.

Search URLs are navigation aids, not observations. Existing Google evidence is
not reused as a new check, and no Google-derived address or coordinate is added.
Marker proximity is not an exact-building survey or proof of current Bass.
Keep receipt files and generated queues outside the public repository.
"""
import argparse
import copy
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

from directory_release import active_evidence
from owner_location_review import classify_review, current_confirmation


EVIDENCE_CATEGORIES = (
    'named_place_identity_consistent',
    'named_place_identity_and_pin_consistent',
    'named_place_identity_conflict', 'named_place_pin_conflict',
    'trading_status_conflict', 'ambiguous_results', 'no_named_place',
    'unavailable',
)
IDENTITY_FIELDS = ('venue_id', 'pub_name', 'place_name', 'postcode', 'area', 'country')
REFERENCE_HOSTS = {
    'camra': frozenset(('camra.org.uk', 'www.camra.org.uk')),
    'fsa': frozenset(('ratings.food.gov.uk', 'api.ratings.food.gov.uk')),
    'fhrs': frozenset(('ratings.food.gov.uk', 'api.ratings.food.gov.uk')),
    'openstreetmap': frozenset(('openstreetmap.org', 'www.openstreetmap.org')),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def signatures(row, location):
    identity = digest({key: row.get(key, '') for key in IDENTITY_FIELDS})
    point = digest({key: location.get(key) for key in ('lat', 'lng', 'precision')})
    return dict(identity_signature=identity, point_signature=point,
                binding_signature=digest([identity, point]))


def source_url(value):
    parsed = urlparse(str(value or ''))
    return str(value) if parsed.scheme in ('https', 'http') and parsed.netloc else ''


def non_google(item):
    # Operator sources need an explicit host allowlist before copying addresses.
    parsed = urlparse(item.get('url', ''))
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.netloc.lower() in REFERENCE_HOSTS.get(item.get('source'), ()))


def valid_google_result_url(value, require_named_place):
    parsed = urlparse(value)
    if (parsed.scheme != 'https' or parsed.netloc.lower() not in
            ('google.com', 'www.google.com', 'maps.google.com')
            or any(ord(c) < 32 for c in value)):
        return False
    path = unquote(parsed.path)
    parts = path.split('/')
    if any(p in ('.', '..') for p in parts) or '\\' in path or '//' in path:
        return False
    if path.startswith('/maps/place/'):
        slug = parts[3]
        # A coordinate-only slug or @camera position is not a named place.
        return bool(slug and not slug.startswith('@') and re.search(r'[^\W\d_]', slug)
                    and not re.search(r'%[0-9a-fA-F]{2}', slug))
    return not require_named_place and path.rstrip('/') == '/maps/search'


def build_queue(bundle, registry, fsa_report):
    """Return exactly one pending record for each active release row."""
    rows = bundle['rows']
    identifiers = [row['venue_id'] for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError('Duplicate active venue IDs')
    reports = {}
    for record in fsa_report.get('records', []):
        identifier = record['venue_id']
        if identifier in reports:
            raise ValueError('Duplicate FSA report venue ID: ' + identifier)
        reports[identifier] = record
    queue = []
    for row in rows:
        identifier = row['venue_id']
        entry = registry['venues'][identifier]
        location = bundle['locations'][identifier]
        pin = entry.get('verified_pin') or {}
        evidence = active_evidence(pin)
        audit = reports.get(identifier, {})
        fresh = bool(audit and audit.get('pin') == {k: pin.get(k) for k in ('lat', 'lng')}
                     and audit.get('listing') == {k: row.get(k) for k in ('pub_name', 'postcode')})
        comparisons = audit.get('comparisons', []) if fresh else []
        differences = [float(c['difference_metres']) for c in comparisons if c.get('difference_metres') is not None]
        largest = max(differences, default=0)
        references = [dict(address=e['reference_address'], postcode=e.get('reference_postcode', ''),
                           source=e['source'], url=source_url(e['url']))
                      for e in evidence if e.get('reference_address') and non_google(e)]
        references += [dict(address=c['address'], postcode=c.get('postcode', ''), source='fsa',
                            url=source_url(c['url'])) for c in comparisons
                       if c.get('address') and (urlparse(c.get('url', '')).hostname or '').lower() == 'ratings.food.gov.uk']
        query_parts = [row['pub_name']]
        if references:
            query_parts += [references[0]['address']]
        query_parts += [row.get('place_name', ''), row.get('postcode', ''), 'United Kingdom']
        query = ', '.join(dict.fromkeys(p for p in query_parts if p))
        urls = set()
        for e in evidence + comparisons + (entry.get('trading_status_review') or {}).get('evidence', []):
            if source_url(e.get('url')):
                urls.add(e['url'])
        attempt = entry.get('verification_attempt') or {}
        for value in (attempt.get('sources') or {}).values():
            if source_url(value.get('url')):
                urls.add(value['url'])
        issue = pin.get('metadata_issue') or {}
        if source_url(issue.get('source_url')):
            urls.add(issue['source_url'])
        review = registry.get('location_review', {}).get('records', {}).get(identifier, {})
        owner_pin_confirmed = bool(current_confirmation(row, pin))
        review_assessment = classify_review(review) if review else dict(
            review_category='informational', manual_action_required=False, next_action='No pin check requested')
        review_action = bool(review.get('active') and review_assessment['manual_action_required'])
        manual_action_required = bool(pin.get('review_required') or location['precision'] != 'venue' or review_action)
        if review_action:
            review_category, next_action = review_assessment['review_category'], review_assessment['next_action']
        elif pin.get('review_required') or location['precision'] != 'venue':
            review_category = 'needs_verification'
            next_action = 'Verify the displayed location from independent evidence; this does not establish that the pin is wrong.'
        elif review.get('active') and review_assessment['review_category'] == 'source_warning':
            review_category = 'source_warning'
            next_action = 'Source unavailable; retry the source; no pub pin check requested.'
        else:
            review_category, next_action = 'informational', 'No pin check requested'
        if owner_pin_confirmed and not review_action and not pin.get('review_required') and location['precision'] == 'venue':
            review_category, next_action = 'confirmed', 'No pin check requested'
        quarantine = bool(pin.get('review_required') or location['precision'] != 'venue' or review.get('active'))
        acknowledged = bool(entry.get('acknowledged_trading_review') or issue.get('owner_review'))
        owner_history = bool(entry.get('location_approvals') or entry.get('identity_reviews') or entry.get('location_corrections'))
        confidence = audit.get('confidence', 'not_checked') if fresh else 'not_checked'
        risks = []
        for condition, label in ((quarantine, 'active_quarantine_or_review'),
                                 (acknowledged, 'acknowledged_conflict'),
                                 (largest > 50, 'large_fsa_difference'),
                                 (confidence in ('not_checked', 'existing_evidence_needs_corroboration'), 'uncorroborated'),
                                 (owner_history, 'owner_review_history')):
            if condition:
                risks.append(label)
        priority = next((n for n, flag in enumerate((quarantine, acknowledged, largest > 50,
                                                    'uncorroborated' in risks, owner_history)) if flag), 5)
        item = {key: row.get(key, '') for key in IDENTITY_FIELDS}
        item.update(signatures(row, location))
        item.update(current_pin={k: location[k] for k in ('lat', 'lng')}, precision=location['precision'],
                    current_pin_source=pin.get('source', '') if location['precision'] == 'venue' else 'postcode_fallback',
                    reference_addresses=references, google_named_search_query=query,
                    google_named_search_url='https://www.google.com/maps/search/?' + urlencode({'api': 1, 'query': query}),
                    current_pin_url='https://www.google.com/maps/search/?' + urlencode({'api': 1, 'query': f"{location['lat']},{location['lng']}"}),
                    source_urls=sorted(urls), camra_urls=sorted(u for u in urls if urlparse(u).hostname == 'camra.org.uk'),
                    fsa_urls=sorted(u for u in urls if urlparse(u).hostname in ('ratings.food.gov.uk', 'api.ratings.food.gov.uk')),
                    confidence=confidence, fsa_status=audit.get('status', 'not_checked') if fresh else 'stale_or_missing',
                    fsa_checked_at=audit.get('checked_at', '') if fresh else '', largest_fsa_difference_metres=largest or None,
                    issues=list(dict.fromkeys(review.get('reasons', []) + (audit.get('reasons', []) if fresh else []))),
                    warnings=audit.get('warnings', []) if fresh else [], risk_flags=risks, priority=priority,
                    owner_review_history=owner_history, acknowledged_conflict=acknowledged,
                    review_category=review_category, manual_action_required=manual_action_required,
                    next_action=next_action, owner_pin_confirmed=owner_pin_confirmed,
                    google_check_status='pending', google_verified=False)
        queue.append(item)
    queue.sort(key=lambda q: (q['priority'], -(q['largest_fsa_difference_metres'] or 0), q['pub_name'].casefold(), q['venue_id']))
    for rank, item in enumerate(queue, 1):
        item['risk_rank'] = rank
    return queue


def make_receipt(item, category, checked_at, reviewer, result_url, today=None):
    """Minimal private observation: no copied addresses, points or free text.

    A receipt records a human observation, not coordinate authority. It cannot
    move a pin, resolve a lifecycle review, or verify current Bass availability.
    """
    if category not in EVIDENCE_CATEGORIES:
        raise ValueError('Unknown evidence category')
    checked = date.fromisoformat(checked_at)
    if checked > (today or date.today()) or not reviewer.strip() or not source_url(result_url):
        raise ValueError('Receipt requires a past/current date, reviewer and result URL')
    require_named = category.startswith('named_place_') or category == 'trading_status_conflict'
    if not valid_google_result_url(result_url, require_named):
        raise ValueError('Expected a named Google Maps place URL' if require_named else 'Expected a Google Maps place/search URL')
    return dict(schema_version=1, venue_id=item['venue_id'],
                **{k: item[k] for k in ('identity_signature', 'point_signature', 'binding_signature')},
                category=category, checked_at=checked_at, reviewer=reviewer.strip(), result_url=result_url,
                identity_observed=category in ('named_place_identity_consistent', 'named_place_identity_and_pin_consistent', 'named_place_identity_conflict'),
                pin_observed=category in ('named_place_identity_and_pin_consistent', 'named_place_pin_conflict'),
                scope='private_manual_observation_only', pin_promotion_authorized=False)


def receipt_is_current(receipt, item):
    """Current means bound to this identity/point; consult checked_at for age."""
    try:
        validated = make_receipt(item, receipt['category'], receipt['checked_at'], receipt['reviewer'], receipt['result_url'])
        return (receipt.get('schema_version') == 1 and all(receipt.get(k) == validated[k] for k in
                ('venue_id', 'identity_signature', 'point_signature', 'binding_signature', 'scope', 'pin_promotion_authorized',
                 'identity_observed', 'pin_observed')))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def apply_receipts(queue, receipts):
    """Return (copied queue, summary), retaining only exact-bound observations.

    Malformed or duplicate ID/category records fail closed. Unknown IDs and
    stale signatures are reported as discarded; they never mark a row observed.
    Multiple distinct observations can coexist without resolving their conflict.
    """
    if not isinstance(receipts, list):
        raise ValueError('Receipts must be a JSON list')
    result = copy.deepcopy(queue)
    by_id = {item['venue_id']: item for item in result}
    if len(by_id) != len(result):
        raise ValueError('Duplicate queue venue IDs')
    for item in result:
        item.update(google_check_status='pending', google_verified=False,
                    google_observation_categories=[], google_checks=[], receipt_rejections=[])
        item['risk_flags'] = [flag for flag in item['risk_flags'] if flag != 'google_observation_conflict']
    seen, discarded, accepted = set(), [], 0
    for index, receipt in enumerate(receipts):
        try:
            if not isinstance(receipt, dict):
                raise ValueError('Receipt must be an object')
            identifier, category = receipt['venue_id'], receipt['category']
            if not isinstance(identifier, str) or not identifier:
                raise ValueError('Missing venue ID')
            for key in ('identity_signature', 'point_signature', 'binding_signature'):
                if not isinstance(receipt[key], str) or not re.fullmatch('[0-9a-f]{64}', receipt[key]):
                    raise ValueError('Invalid signature: ' + key)
            expected = make_receipt(receipt, category, receipt['checked_at'], receipt['reviewer'], receipt['result_url'])
            if receipt != expected:
                raise ValueError('Receipt fields differ from the supported schema')
            if receipt['binding_signature'] != digest([receipt['identity_signature'], receipt['point_signature']]):
                raise ValueError('Inconsistent binding signature')
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ValueError(f'Malformed receipt at index {index}: {error}') from error
        pair = (identifier, category)
        if pair in seen:
            raise ValueError(f'Duplicate receipt ID/category: {identifier}/{category}')
        seen.add(pair)
        item = by_id.get(identifier)
        if item is None:
            discarded.append(dict(venue_id=identifier, category=category, reason='unknown_active_venue_id'))
        elif not receipt_is_current(receipt, item):
            reason = dict(venue_id=identifier, category=category, reason='identity_or_point_signature_changed')
            discarded.append(reason)
            item['receipt_rejections'].append(reason)
        else:
            item['google_check_status'] = 'observed'
            item['google_observation_categories'].append(category)
            item['google_checks'].append(copy.deepcopy(receipt))
            if category in ('named_place_pin_conflict', 'named_place_identity_conflict', 'trading_status_conflict'):
                if 'google_observation_conflict' not in item['risk_flags']:
                    item['risk_flags'].insert(0, 'google_observation_conflict')
            accepted += 1
    result.sort(key=lambda q: ('google_observation_conflict' not in q['risk_flags'],
                               q['priority'], -(q['largest_fsa_difference_metres'] or 0),
                               q['pub_name'].casefold(), q['venue_id']))
    for rank, item in enumerate(result, 1):
        item['risk_rank'] = rank
    summary = dict(input_receipt_count=len(receipts), accepted_receipt_count=accepted,
                   discarded_receipt_count=len(discarded), discarded_receipts=discarded,
                   observed_venue_count=sum(q['google_check_status'] == 'observed' for q in result),
                   pending_venue_count=sum(q['google_check_status'] == 'pending' for q in result),
                   conflict_venue_count=sum('google_observation_conflict' in q['risk_flags'] for q in result),
                   accepted_category_counts=dict(Counter(check['category'] for q in result for check in q['google_checks'])),
                   rejected_reason_counts=dict(Counter(d['reason'] for d in discarded)))
    return result, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, help='Optional private JSON list of manual observations')
    args = parser.parse_args()
    read = lambda name: json.loads((args.root / name).read_text(encoding='utf-8'))
    bundle = read('directory-release.json')
    queue = build_queue(bundle, read('venue-registry.json'), read('audit/full-location-audit.json'))
    receipts = json.loads(args.receipts.read_text(encoding='utf-8')) if args.receipts else []
    queue, receipt_summary = apply_receipts(queue, receipts)
    summary = dict(schema_version=1, release_id=bundle['release_id'], generated_at=date.today().isoformat(),
                   active_count=len(bundle['rows']), queue_count=len(queue),
                   unique_id_count=len({q['venue_id'] for q in queue}),
                   google_status_counts=dict(Counter(q['google_check_status'] for q in queue)),
                   manual_action_count=sum(q['manual_action_required'] for q in queue),
                   receipt_summary=receipt_summary,
                   risk_counts=dict(Counter(flag for q in queue for flag in q['risk_flags'])),
                   evidence_categories=EVIDENCE_CATEGORIES,
                   policy='Offline private queue; links are not Google checks; no automatic pin promotion.')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in [('queue.json', dict(summary=summary, records=queue)), ('summary.json', summary)]:
        (args.output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (args.output_dir / 'queue.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(queue[0]) if queue else list(IDENTITY_FIELDS))
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in q.items()} for q in queue)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
