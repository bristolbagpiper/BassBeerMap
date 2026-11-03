"""Consume private Hermes browser observations into the Google review status.

Receipts are local inputs. Public status contains only the directory's own
listing fields and bounded review classifications; it never copies Google data.
"""
import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from audit_google_places import COVERAGE_ISSUE
from build_place_review_queue import build_queue, digest, valid_google_result_url
from directory_release import read_json, write_json

MAX_AGE = timedelta(days=30)
CATEGORIES = frozenset(('identity_and_pin_consistent', 'identity_conflict', 'pin_conflict',
                        'trading_conflict', 'ambiguous', 'unavailable'))
OBSERVATION_FIELDS = frozenset(('schema_version', 'provider', 'venue_id', 'identity_signature',
                                'point_signature', 'binding_signature', 'category', 'attempted_at',
                                'checked_at', 'result_url', 'source_url'))
CONFLICTS = frozenset(('identity_conflict', 'pin_conflict', 'trading_conflict'))


def parse_utc(value, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Timestamp must be an ISO-8601 UTC string')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Malformed timestamp') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError('Timestamp must include UTC timezone')
    return parsed.astimezone(timezone.utc)


def valid_google_url(value, place_required):
    return isinstance(value, str) and valid_google_result_url(value, place_required)


def _reason(category):
    return {
        'identity_conflict': 'Google named-place result differs in business identity or postcode; verify the matched venue and address',
        'pin_conflict': 'Google named-place marker differs by more than 50 metres; existing pin retained for review',
        'trading_conflict': 'Google indicates a possible closure; trading status needs review',
        'ambiguous': 'Google results are incomplete or ambiguous; no unique place accepted',
    }.get(category)


def build_status(root, receipts, now=None):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError('Status time must be UTC')
    bundle = read_json(root / 'directory-release.json')
    registry = read_json(root / 'venue-registry.json')
    fsa_path = root / 'audit' / 'full-location-audit.json'
    fsa = read_json(fsa_path, {})
    queue = build_queue(bundle, registry, fsa)
    by_id = {item['venue_id']: item for item in queue}
    if not isinstance(receipts, dict) or set(receipts) != {'schema_version', 'observations'} or type(receipts['schema_version']) is not int or receipts['schema_version'] != 1 or not isinstance(receipts['observations'], list):
        raise ValueError('Invalid Hermes receipt document')
    attempts, unavailable_attempts, conclusive, ambiguous = {}, {}, {}, {}
    seen = set()
    for index, item in enumerate(receipts['observations']):
        if not isinstance(item, dict) or set(item) != OBSERVATION_FIELDS:
            raise ValueError(f'Malformed observation at index {index}: unsupported fields')
        if (type(item['schema_version']) is not int or item['schema_version'] != 1
                or item['provider'] != 'hermes-browser' or item['category'] not in CATEGORIES
                or not isinstance(item['venue_id'], str) or not item['venue_id']):
            raise ValueError(f'Malformed observation at index {index}: invalid metadata')
        attempted = parse_utc(item['attempted_at'])
        checked = parse_utc(item['checked_at'], nullable=True)
        if attempted > now or (checked and (checked > now or checked < attempted)):
            raise ValueError(f'Malformed observation at index {index}: future or reversed dates')
        category = item['category']
        unavailable = category == 'unavailable'
        if (unavailable and checked is not None) or (not unavailable and checked is None):
            raise ValueError(f'Malformed observation at index {index}: invalid checked_at')
        result_url, source_url = item['result_url'], item['source_url']
        if unavailable:
            if result_url != '' or not valid_google_url(source_url, False):
                raise ValueError(f'Malformed observation at index {index}: unavailable requires checked source URL')
        elif not valid_google_url(result_url, category != 'ambiguous') or source_url != '':
            raise ValueError(f'Malformed observation at index {index}: comparison requires a named place, or search URL for ambiguity')
        for field in ('identity_signature', 'point_signature', 'binding_signature'):
            if not isinstance(item[field], str) or not re.fullmatch(r'[0-9a-f]{64}', item[field]):
                raise ValueError(f'Malformed observation at index {index}: invalid signature')
        if item['binding_signature'] != digest([item['identity_signature'], item['point_signature']]):
            raise ValueError(f'Malformed observation at index {index}: inconsistent binding')
        pair = (item['venue_id'], category, item['attempted_at'])
        if pair in seen:
            raise ValueError(f'Malformed observation at index {index}: duplicate attempt')
        seen.add(pair)
        venue = by_id.get(item['venue_id'])
        if not venue or any(item[field] != venue[field] for field in ('identity_signature', 'point_signature', 'binding_signature')):
            continue
        attempts[item['venue_id']] = max(attempts.get(item['venue_id'], attempted), attempted)
        if unavailable:
            unavailable_attempts[item['venue_id']] = max(unavailable_attempts.get(item['venue_id'], attempted), attempted)
        if checked:
            if category in CONFLICTS or category == 'identity_and_pin_consistent':
                prior = conclusive.get(item['venue_id'])
                if prior is None or checked > prior[0]:
                    conclusive[item['venue_id']] = (checked, {category})
                elif checked == prior[0]:
                    prior[1].add(category)
            elif category == 'ambiguous':
                prior = ambiguous.get(item['venue_id'])
                if prior is None or checked > prior:
                    ambiguous[item['venue_id']] = checked

    findings, current = [], 0
    for identifier, (checked, categories) in conclusive.items():
        fresh = timedelta(0) <= now - checked <= MAX_AGE
        if fresh:
            current += 1
        # A conflict set is replaced only by a newer conclusive comparison.
        # Conflicts emitted together at the same check time are all retained.
        reasons = [_reason(category) for category in sorted(categories) if category in CONFLICTS]
        if reasons:
            item = by_id[identifier]
            findings.append(dict(venue_id=identifier, pub_name=item['pub_name'], place_name=item['place_name'],
                                 postcode=item['postcode'], status='review_needed', reason='; '.join(reasons),
                                 binding_signature=item['binding_signature']))
    for identifier, checked in ambiguous.items():
        if identifier not in conclusive or checked > conclusive[identifier][0]:
            item = by_id[identifier]
            findings.append(dict(venue_id=identifier, pub_name=item['pub_name'], place_name=item['place_name'],
                                 postcode=item['postcode'], status='review_needed', reason=_reason('ambiguous'),
                                 binding_signature=item['binding_signature']))
    total = len(queue)
    issues = []
    configured = True
    if current != total:
        issues.append(COVERAGE_ISSUE)
    # A later readable results list proves source recovery without verifying a
    # particular venue or clearing an older unresolved identity/pin conflict.
    available = {identifier: checked for identifier, (checked, _) in conclusive.items()}
    for identifier, checked in ambiguous.items():
        available[identifier] = max(available.get(identifier, checked), checked)
    if any(identifier in unavailable_attempts and
           (identifier not in available or unavailable_attempts[identifier] > available[identifier])
           for identifier in by_id):
        issues.append('Hermes Google browser source was unavailable; previous successful checks were retained')
    status = dict(schema_version=1, checked_at=now.isoformat(), configured=configured,
                  directory_binding_signature=digest(sorted((item['venue_id'], item['binding_signature']) for item in queue)),
                  issues=issues, findings=sorted(findings, key=lambda f: f['venue_id']),
                  counts=dict(total=total, current=current, unchecked_or_stale=total-current,
                              checked_this_run=sum(1 for checked, _ in conclusive.values() if checked.date() == now.date()),
                              attempted_venue_count=len(attempts), review_count=len(findings)), hard_failure=False)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--status', type=Path, required=True)
    args = parser.parse_args(argv)
    status = build_status(args.root, read_json(args.receipts))
    write_json(args.status, status)
    print(json.dumps(status, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
