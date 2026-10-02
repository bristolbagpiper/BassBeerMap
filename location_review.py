"""Persist unresolved locations and their lifecycle independently of notifications."""
import copy
import hashlib
import json
from datetime import date


def build_review(rows, registry, ledger):
    today = date.today().isoformat()
    records = copy.deepcopy(registry.get('location_review', {}).get('records', {}))
    for record in records.values():
        record['active'] = False
        record['unverified'] = False
    for row in rows:
        identifier = row['venue_id']
        entry = registry['venues'][identifier]
        pin = entry.get('verified_pin') or {}
        attempt = entry.get('verification_attempt') or {}
        unverified = ledger['records'][entry['current_key']]['display_precision'] != 'venue'
        reasons = []
        if unverified:
            reasons.append(pin.get('review_reason') or attempt.get('reason') or 'Location has not yet been independently verified')
        issue = pin.get('metadata_issue') or {}
        if issue.get('listed', '').replace(' ', '').upper() != issue.get('reference', '').replace(' ', '').upper():
            reasons.append(f"Directory postcode {issue.get('listed')} conflicts with reference address {issue.get('reference')}")
        if attempt.get('status') == 'unavailable':
            reasons.append(f"Verification source unavailable: {attempt.get('reason', 'unknown error')}")
        if attempt.get('status') == 'manual_review':
            latest = max((e.get('checked_at', '') for e in pin.get('evidence', [])), default='')
            if latest and (date.today() - date.fromisoformat(latest)).days >= 180:
                reasons.append('Manual location evidence needs a fresh address/map check')
        active = bool(reasons)
        if not active and identifier not in records:
            continue
        previous = records.get(identifier, {})
        history = previous.get('history', [])
        status = 'unverified' if unverified else 'review_needed' if active else 'resolved'
        if not history or history[-1]['status'] != status or history[-1]['reasons'] != reasons:
            history.append({'date': today, 'status': status, 'reasons': reasons})
        records[identifier] = dict(venue_id=identifier, pub_name=row['pub_name'], place_name=row['place_name'],
                                  postcode=row['postcode'], active=active, unverified=unverified,
                                  first_seen=previous.get('first_seen', today), status=status, reasons=reasons,
                                  last_checked=attempt.get('checked_at') or max((e.get('checked_at', '') for e in pin.get('evidence', [])), default=None), source_checks=attempt.get('sources', {}),
                                  source_error=attempt.get('error'), reference=issue, history=history)
        records[identifier]['research'] = entry.get('location_research', {})
    active_ids = {row['venue_id'] for row in rows}
    for identifier, record in records.items():
        if identifier not in active_ids and record.get('status') != 'removed':
            record.update(status='removed', reasons=['Venue removed from the current directory'])
            record['history'].append({'date': today, 'status': 'removed', 'reasons': record['reasons']})
    return {'schema_version': 1, 'records': records,
            'unverified_count': sum(r['unverified'] for r in records.values()),
            'active_count': sum(r['active'] for r in records.values())}


def fingerprint(report):
    # Check dates and repeated identical attempts do not create repeated emails.
    active = {identifier: {k: r.get(k) for k in ('pub_name', 'place_name', 'postcode', 'status', 'reasons')}
              for identifier, r in report['records'].items() if r['active']}
    for identifier, record in report['records'].items():
        if identifier not in active:
            continue
        research = record.get('research', {})
        if research.get('proposals'):
            active[identifier]['proposals'] = [{k: p.get(k) for k in ('name', 'postcode', 'address', 'url', 'lat', 'lng')}
                                               for p in research['proposals']]
        if research.get('status') == 'unavailable':
            active[identifier]['research_error'] = research.get('error')
    return hashlib.sha256(json.dumps(active, sort_keys=True).encode()).hexdigest()


def format_report(report):
    lines = [f"Unverified map locations: {report['unverified_count']}",
             f"All active location reviews: {report['active_count']}", '',
             'Brown pins remain approximate until independently verified.', '']
    for identifier, record in sorted(report['records'].items(), key=lambda item: (not item[1]['unverified'], item[1]['pub_name'], item[0])):
        if not record['active']:
            continue
        lines += [f"{record['pub_name']} — {record['place_name']} ({record['postcode']})",
                  f"ID: {identifier} | First seen: {record['first_seen']} | Last checked: {record.get('last_checked') or 'not checked'}",
                  *[f"- {reason}" for reason in record['reasons']]]
        for source, check in record.get('source_checks', {}).items():
            lines.append(f"- {source.upper()}: {check.get('status', 'unknown')} {check.get('url', '')}".strip())
        if record.get('source_error'):
            lines.append(f"- Lookup error: {record['source_error']}")
        research = record.get('research', {})
        proposals = research.get('proposals', [])
        if proposals and record['unverified']:
            lines.append('Candidates only: check the named address and map before approving. Post comments as the repository owner in issue #5.')
            for proposal in proposals:
                lines += [f"- Candidate: {proposal['name']} — {proposal['address']}, {proposal.get('town', '')} ({proposal['postcode']})",
                          f"  Source: {proposal['url']}",
                          f"  Approval command: /verify-location {identifier} {proposal['url']}"]
        if research.get('coverage', {}).get('complete') is False:
            lines.append('- CAMRA discovery returned incomplete results; absence of a candidate is inconclusive.')
        if research.get('status') == 'unavailable':
            lines.append(f"- Candidate research source unavailable: {research.get('error', 'unknown error')}")
        lines.append('')
    lines += ['Review queue: https://github.com/bristolbagpiper/BassBeerMap/issues/5',
              'Full history: https://github.com/bristolbagpiper/BassBeerMap/blob/main/location-review.json']
    return '\n'.join(lines)
