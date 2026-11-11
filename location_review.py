"""Persist unresolved locations and their lifecycle independently of notifications."""
import copy
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from build_place_review_queue import digest, signatures
from directory_release import active_evidence
from owner_location_review import classify_review, current_confirmation, google_finding_assessment, pending_building_review


GOOGLE_COVERAGE_ISSUE = 'Google comparison coverage is incomplete; untested, stale or inconclusive pubs remain'
GOOGLE_STALE_STATUS_ISSUE = 'Google comparison status is missing or stale; rerun the daily map audit'
GOOGLE_CONFIGURATION_ISSUE = 'Google comparison is not configured; no live Google checks ran this time'


def postcode_issue_reviewed(issue, pin, pub_name):
    """A human decision covers only the exact postcode pair, venue and point."""
    review = issue.get('owner_review') or {}
    compact = lambda value: str(value or '').replace(' ', '').upper()
    return bool(review.get('checked_at') and review.get('reviewed_by') and review.get('owner_instruction')
                and review.get('pub_name') == pub_name
                and compact(review.get('listed')) == compact(issue.get('listed'))
                and compact(review.get('reference')) == compact(issue.get('reference'))
                and review.get('source_url') == issue.get('source_url')
                and review.get('approved_coordinate') == {key: pin.get(key) for key in ('lat', 'lng')})


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
        requested = pending_building_review(row, pin)
        if requested:
            reasons.append('Building comparison needs verification: ' + requested['reason'])
        if unverified:
            reasons.append(pin.get('review_reason') or attempt.get('reason') or 'Location has not yet been independently verified')
        issue = pin.get('metadata_issue') or {}
        if (issue.get('listed', '').replace(' ', '').upper() != issue.get('reference', '').replace(' ', '').upper()
                and not postcode_issue_reviewed(issue, pin, row['pub_name'])):
            reasons.append(f"Directory postcode {issue.get('listed')} conflicts with reference address {issue.get('reference')}")
        if attempt.get('status') == 'unavailable':
            reasons.append(f"Verification source unavailable: {attempt.get('reason', 'unknown error')}")
        if attempt.get('status') == 'manual_review':
            confirmation = current_confirmation(row, pin)
            dates = [e.get('checked_at', '') for e in active_evidence(pin)]
            if confirmation:
                dates.append(confirmation['checked_at'])
            latest = max(dates, default='')
            if latest and (date.today() - date.fromisoformat(latest)).days >= 180:
                reasons.append('Manual location evidence needs a fresh address/map check')
        independent = entry.get('independent_location_check', {})
        independent_current = (independent.get('pin') == {key: pin.get(key) for key in ('lat', 'lng')}
                               and independent.get('listing') == {key: row.get(key) for key in ('pub_name', 'postcode')})
        if independent_current:
            # FSA coordinate differences alone cannot invalidate accepted evidence.
            if independent.get('status') == 'unavailable':
                reasons.append('Independent FSA comparison unavailable; last accepted pin retained')
        trading = entry.get('trading_status_review') or {}
        trading_snapshot = {key: trading.get(key) for key in ('status', 'reason', 'evidence')}
        acknowledged = entry.get('acknowledged_trading_review') or {}
        if trading.get('status') == 'needs_review' and acknowledged.get('snapshot') != trading_snapshot:
            reasons.append('Trading status needs review: ' + trading.get('reason', 'conflicting opening or closure information'))
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
                                  last_checked=max([attempt.get('checked_at') or '', independent.get('checked_at', '') if independent_current else '', *[e.get('checked_at', '') for e in pin.get('evidence', [])]]) or None,
                                  source_checks=dict(attempt.get('sources', {}), **({'fsa_independent': dict(status=independent['status'], url=independent.get('reference', {}).get('url', ''))} if independent_current else {})),
                                  source_error=attempt.get('error'), reference=issue, history=history)
        records[identifier]['research'] = entry.get('location_research', {})
        if requested:
            records[identifier]['manual_review_request'] = copy.deepcopy(requested)
        else:
            records[identifier].pop('manual_review_request', None)
        records[identifier].update(classify_review(records[identifier]))
        if trading:
            records[identifier]['trading_status_review'] = copy.deepcopy(trading)
    active_ids = {row['venue_id'] for row in rows}
    for identifier, record in records.items():
        if identifier not in active_ids and record.get('status') != 'removed':
            record.update(status='removed', reasons=['Venue removed from the current directory'])
            record['history'].append({'date': today, 'status': 'removed', 'reasons': record['reasons']})
    return {'schema_version': 1, 'records': records,
            'unverified_count': sum(r['unverified'] for r in records.values()),
            'actionable_count': sum(r.get('manual_action_required', r['active']) and r['active'] for r in records.values()),
            'source_warning_count': sum(r.get('review_category') == 'source_warning' and r['active'] for r in records.values()),
            'active_count': sum(r['active'] for r in records.values())}


def load_health_status(path):
    path = Path(path)
    if not path.exists():
        return None
    status = json.loads(path.read_text(encoding='utf-8'))
    if status is None:
        raise ValueError('Invalid map health status: null snapshot')
    return status


def with_health(report, status):
    """Combine the current health snapshot without changing venue lifecycle data."""
    combined = copy.deepcopy(report)
    if status is None:  # Older checkouts have no health snapshot yet.
        return combined
    if (not isinstance(status, dict) or status.get('schema_version') != 1
            or not isinstance(status.get('issues'), list)
            or not all(isinstance(issue, str) and issue for issue in status['issues'])):
        raise ValueError('Invalid map health status; rerun the daily map audit')
    combined['health_issues'] = sorted(set(status['issues']))
    combined['health_count'] = len(combined['health_issues'])
    combined['health_run_id'] = status.get('run_id', '')
    combined['health_run_attempt'] = status.get('run_attempt', 1)
    combined['health_checked_at'] = status.get('checked_at', '')
    combined['health_run_url'] = status.get('run_url', '')
    return combined


def load_google_status(path):
    path = Path(path)
    provider_path = path.parent / 'google-review-provider.json'
    if provider_path.exists():
        provider = json.loads(provider_path.read_text(encoding='utf-8'))
        if (not isinstance(provider, dict) or set(provider) != {'provider'}
                or provider['provider'] not in ('places-api', 'hermes-browser', 'hermes-local')):
            raise ValueError('Invalid Google review provider configuration')
        if provider == {'provider': 'hermes-local'}:
            # Google review and private notification happen in the local
            # Hermes execution. Ignore retained public snapshots in this mode.
            return {'_google_handled_locally': True}
        if provider == {'provider': 'hermes-browser'}:
            # A newly published browser batch must reach the same review issue
            # immediately, without waiting for the next scheduled API/router run.
            browser_status = path.parent / 'hermes-google-status.json'
            if browser_status.exists():
                path = browser_status
    if not path.exists():
        return None  # Older checkouts have no Google snapshot yet.
    status = json.loads(path.read_text(encoding='utf-8'))
    if status is None:
        raise ValueError('Invalid Google comparison status: null snapshot')
    return status


def with_google(report, status, current_bundle=None, now=None):
    """Attach derived Google review flags, never provider names or coordinates."""
    combined = copy.deepcopy(report)
    if status == {'_google_handled_locally': True}:
        return combined
    if status is None:
        # Google is a required comparison source. Absence of a saved run is a
        # coverage gap, never an all-clear or a reason to recheck correct pins.
        combined.update(google_issues=[GOOGLE_COVERAGE_ISSUE, GOOGLE_STALE_STATUS_ISSUE],
                        google_findings=[], google_count=2, google_configured=False,
                        google_actionable_count=0, google_source_warning_count=2,
                        google_hard_failure=False)
        return combined
    fields = {'venue_id', 'pub_name', 'place_name', 'postcode', 'status', 'reason', 'binding_signature'}
    optional_fields = {'checked_at', 'current_pin', 'business_status'}
    if (not isinstance(status, dict) or type(status.get('schema_version')) is not int
            or status['schema_version'] != 1 or not isinstance(status.get('configured'), bool)
            or not isinstance(status.get('hard_failure', False), bool)
            or not isinstance(status.get('issues'), list)
            or not all(isinstance(issue, str) and issue for issue in status['issues'])
            or not isinstance(status.get('counts'), dict)
            or not all(isinstance(key, str) and type(value) is int and value >= 0
                       for key, value in status['counts'].items())
            or not isinstance(status.get('findings'), list)
            or not all(isinstance(finding, dict) and fields <= finding.keys() <= fields | optional_fields
                       and all(isinstance(finding[key], str) for key in fields)
                       and all(finding[key] for key in ('venue_id', 'pub_name', 'status', 'reason'))
                       and re.fullmatch(r'[0-9a-f]{64}', finding['binding_signature'])
                       for finding in status['findings'])):
        raise ValueError('Invalid Google comparison status; rerun the daily map audit')
    directory_signature = status.get('directory_binding_signature')
    if (not isinstance(directory_signature, str) or not re.fullmatch(r'[0-9a-f]{64}', directory_signature)):
        if directory_signature is not None or not status.get('hard_failure'):
            raise ValueError('Invalid Google comparison status; missing directory binding')
    issues = set(status['issues'])
    if not status['configured']:
        issues.add(GOOGLE_CONFIGURATION_ISSUE)
        issues.add(GOOGLE_COVERAGE_ISSUE)
    current = {}
    if current_bundle is not None:
        try:
            rows, locations = current_bundle['rows'], current_bundle['locations']
            if not isinstance(rows, list) or not isinstance(locations, dict):
                raise ValueError('Invalid current release')
            for row in rows:
                identifier = row['venue_id']
                if not isinstance(identifier, str) or identifier in current or not isinstance(locations[identifier], dict):
                    raise ValueError('Invalid current release venue')
                current[identifier] = (row, signatures(row, locations[identifier])['binding_signature'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Invalid current release for Google comparison status') from error
        current_signature = digest(sorted((identifier, value[1]) for identifier, value in current.items()))
        if current_signature != directory_signature:
            issues.add(GOOGLE_COVERAGE_ISSUE)
    elif status['findings']:
        raise ValueError('Current release is required to validate Google comparison findings')
    else:
        issues.add(GOOGLE_COVERAGE_ISSUE)
    findings = []
    for finding in status['findings']:
        row_and_signature = current.get(finding['venue_id'])
        if row_and_signature is None or finding['binding_signature'] != row_and_signature[1]:
            issues.add(GOOGLE_COVERAGE_ISSUE)
            continue
        row = row_and_signature[0]
        finding = {field: finding[field] for field in fields}
        findings.append(dict(finding, **{field: row.get(field, '') for field in ('pub_name', 'place_name', 'postcode')}))
    try:
        checked_at = datetime.fromisoformat(status.get('checked_at', '').replace('Z', '+00:00'))
        age = (now or datetime.now(timezone.utc)) - checked_at
        stale = checked_at.tzinfo is None or not timedelta(0) <= age <= timedelta(days=30)
    except (TypeError, ValueError, AttributeError):
        stale = True
    if stale:
        issues.update((GOOGLE_STALE_STATUS_ISSUE, GOOGLE_COVERAGE_ISSUE))
    combined['google_issues'] = sorted(issues)
    # Only the directory's own listing and derived review flags are reportable.
    combined['google_findings'] = sorted(findings,
                                         key=lambda finding: json.dumps(finding, sort_keys=True))
    for finding in combined['google_findings']:
        finding.update(google_finding_assessment(finding))
    combined['google_actionable_count'] = sum(f['manual_action_required'] for f in combined['google_findings'])
    combined['google_source_warning_count'] = len(combined['google_issues']) + sum(not f['manual_action_required'] for f in combined['google_findings'])
    combined['google_count'] = len(combined['google_issues']) + len(combined['google_findings'])
    combined['google_configured'] = status['configured']
    combined['google_counts'] = copy.deepcopy(status['counts'])
    if current_bundle is not None and current_signature != directory_signature:
        combined['google_counts'].update(total=len(current), current=0, unchecked_or_stale=len(current))
        if 'attempted_venue_count' in combined['google_counts']:
            combined['google_counts']['attempted_venue_count'] = 0
    combined['google_provider'] = status.get('provider', 'places-api')
    combined['google_hard_failure'] = status.get('hard_failure', False)
    for field in ('run_id', 'run_attempt', 'checked_at', 'run_url'):
        combined['google_' + field] = status.get(field, '')
    return combined


def fingerprint(report):
    # Check dates and repeated identical attempts do not create repeated emails.
    active = {identifier: {k: r.get(k) for k in ('pub_name', 'place_name', 'postcode', 'status', 'reasons', 'review_category', 'manual_action_required')}
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
    if report.get('health_issues'):
        # Run IDs and timestamps are receipts, not new incidents.
        active['map_health'] = sorted(set(report['health_issues']))
    if report.get('google_issues') or report.get('google_findings'):
        active['google_comparison'] = dict(issues=sorted(set(report.get('google_issues', []))),
            findings=sorted(report.get('google_findings', []), key=lambda finding: json.dumps(finding, sort_keys=True)))
    return hashlib.sha256(json.dumps(active, sort_keys=True).encode()).hexdigest()


def format_report(report, include_google_source_details=False):
    records = report.get('records', {})
    actions = [(identifier, record) for identifier, record in records.items()
               if record.get('active') and record.get('manual_action_required', classify_review(record)['manual_action_required'])]
    warnings = [(identifier, record) for identifier, record in records.items()
                if record.get('active') and record.get('review_category', classify_review(record)['review_category']) == 'source_warning']
    information = [(identifier, record) for identifier, record in records.items()
                   if not record.get('active')]
    action_count = report.get('actionable_count', len(actions))
    lines = [f"Unverified map locations: {report.get('unverified_count', 0)}",
             f"All active location reviews: {report.get('active_count', len(actions) + len(warnings))}",
             f"Action required: {action_count}",
             f"Source warnings — no pin check requested: {report.get('source_warning_count', len(warnings))}", '',
             'Brown pins remain approximate until independently verified.', '']
    if 'health_issues' in report:
        lines += [f"Active map health checks: {len(report['health_issues'])}",
                  *[f'- {issue}' for issue in report['health_issues']]]
        if report.get('health_run_url'):
            lines.append('Latest map audit: ' + report['health_run_url'])
        lines.append('')
    if 'google_issues' in report:
        findings = report.get('google_findings', [])
        counts = report.get('google_counts', {})
        if 'attempted_venue_count' in counts and 'total' in counts:
            lines.append(f"Google listings with browser attempts recorded: {counts['attempted_venue_count']}/{counts['total']}")
        if 'current' in counts and 'total' in counts:
            lines.append(f"Google comparisons current for this listing and pin: {counts['current']}/{counts['total']}")
        google_actions = [f for f in findings if f.get('manual_action_required', google_finding_assessment(f)['manual_action_required'])]
        google_warnings = [f for f in findings if not f.get('manual_action_required', google_finding_assessment(f)['manual_action_required'])]
        lines += [f"Google pub actions: {report.get('google_actionable_count', len(google_actions))}",
                  f"Google source warnings — no pin check requested: {report.get('google_source_warning_count', len(google_warnings))}"]
        for issue in report['google_issues']:
            lines.append(f'- Google operational status: {issue}')
        if google_actions:
            lines += ['', '## Google actions', '']
        for finding in google_actions:
            lines += [f"- {finding['pub_name']} — {finding['place_name']} ({finding['postcode']})",
                      f"  ID: {finding['venue_id']} | {finding['status']}: {finding['reason']}",
                      f"  Category: {finding.get('review_category', google_finding_assessment(finding)['review_category'])}; {finding.get('next_action', google_finding_assessment(finding)['next_action'])}"]
        if google_warnings:
            lines += ['', '## Google source warnings — no pin check requested', '']
            if include_google_source_details:
                for finding in google_warnings:
                    lines.append(f"- {finding['pub_name']} — {finding['place_name']} ({finding['postcode']}): {finding['reason']}")
            else:
                filename = 'hermes-google-status.json' if report.get('google_provider') == 'hermes-browser' else 'google-places-status.json'
                lines.append(f"{len(google_warnings)} inconclusive pub results. Full source details: https://github.com/bristolbagpiper/BassBeerMap/blob/main/{filename}")
        lines.append('Google comparisons are diagnostic; they do not approve or move map pins.')
        if report.get('google_run_url'):
            lines.append('Latest Google comparison: ' + report['google_run_url'])
        lines.append('')
    def render_record(identifier, record):
        lines.extend([f"{record['pub_name']} — {record['place_name']} ({record['postcode']})",
                      f"ID: {identifier} | First seen: {record['first_seen']} | Last checked: {record.get('last_checked') or 'not checked'}",
                      *[f"- {reason}" for reason in record['reasons']]])
        lines.append(f"- Next action: {record.get('next_action', classify_review(record)['next_action'])}")
        if record.get('manual_review_request'):
            lines.append('- Manual comparison source: ' + record['manual_review_request']['source_url'])
        for source, check in record.get('source_checks', {}).items():
            lines.append(f"- Last recorded automatic verification {source.upper()}: {check.get('status', 'unknown')} {check.get('url', '')}".strip())
        trading = record.get('trading_status_review', {})
        if trading.get('status') == 'needs_review':
            for evidence in trading.get('evidence', []):
                lines.append(f"- Trading status evidence: {evidence.get('claim', '')} {evidence.get('url', '')}")
        if record.get('source_error'):
            lines.append(f"- Lookup error: {record['source_error']}")
        research = record.get('research', {})
        proposals = research.get('proposals', [])
        if research and record['unverified']:
            lines.append(f"- Independent CAMRA research: {research.get('status')}; checked {research.get('checked_at', 'unknown')}")
        if proposals and record['unverified']:
            lines.append('Candidates only: check the named address and map before approving. Post comments as the repository owner in issue #5.')
            for proposal in proposals:
                lines.extend([f"- Candidate: {proposal['name']} — {proposal['address']}, {proposal.get('town', '')} ({proposal['postcode']})",
                              f"  Source: {proposal['url']}",
                              f"  Approval command: /verify-location {identifier} {proposal['url']}"])
        if research.get('coverage', {}).get('complete') is False:
            lines.append('- CAMRA discovery returned incomplete results; absence of a candidate is inconclusive.')
        if research.get('status') == 'unavailable':
            lines.append(f"- Candidate research source unavailable: {research.get('error', 'unknown error')}")
        lines.append('')
    lines += ['## Action required', '']
    for identifier, record in sorted(actions, key=lambda item: (not item[1].get('unverified'), item[1].get('pub_name', ''), item[0])):
        render_record(identifier, record)
    lines += ['## Source warnings — no pin check requested', '']
    for identifier, record in sorted(warnings, key=lambda item: (item[1].get('pub_name', ''), item[0])):
        render_record(identifier, record)
    lines += ['## Information', '']
    lines += [f'{len(information)} resolved or confirmed historical records: no pin check requested.',
              'Full source differences and history are available in the linked audit; they are not a manual task list.', '']
    lines += ['Review queue: https://github.com/bristolbagpiper/BassBeerMap/issues/5',
              'Full history: https://github.com/bristolbagpiper/BassBeerMap/blob/main/location-review.json',
              'Full independent audit: https://github.com/bristolbagpiper/BassBeerMap/blob/main/audit/full-location-audit.md']
    return '\n'.join(lines)

