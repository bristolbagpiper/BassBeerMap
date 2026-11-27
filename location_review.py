"""Persist unresolved locations and their lifecycle independently of notifications."""
import copy
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from build_place_review_queue import digest, signatures
from urllib.parse import urlsplit

from directory_release import active_evidence, premises_review_valid
from owner_location_review import classify_review, current_confirmation, google_finding_assessment, pending_building_review


GOOGLE_COVERAGE_ISSUE = 'Google comparison coverage is incomplete; untested, stale or inconclusive pubs remain'
GOOGLE_STALE_STATUS_ISSUE = 'Google comparison status is missing or stale; rerun the daily map audit'
GOOGLE_CONFIGURATION_ISSUE = 'Google comparison is not configured; no live Google checks ran this time'


def postcode_issue_reviewed(issue, pin, pub_name, row=None):
    """Recognise the owner's decision or documented addresses for the same premises."""
    if row and premises_review_valid(row, pin):
        premises = pin['premises_review']
        address = premises.get('postal_address') or {}
        sources = address.get('sources') or []
        if (premises['precision'] in ('building', 'site') and address.get('listed') == row.get('postcode') == issue.get('listed')
                and address.get('reference') == issue.get('reference') and address.get('source_url') == issue.get('source_url')
                and isinstance(sources, list) and len({urlsplit(u).hostname for u in sources if u in premises['sources']}) >= 2):
            return True
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
                and not postcode_issue_reviewed(issue, pin, row['pub_name'], row)):
            reasons.append(f"Directory postcode {issue.get('listed')} conflicts with reference address {issue.get('reference')}")
        if attempt.get('status') == 'unavailable':
            reasons.append(f"Verification source unavailable: {attempt.get('reason', 'unknown error')}")
        if attempt.get('status') == 'manual_review' and not premises_review_valid(row, pin):
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
        trading_acknowledged = acknowledged.get('snapshot') == trading_snapshot
        if trading.get('status') == 'needs_review' and not trading_acknowledged:
            reasons.append('Trading status needs review: ' + trading.get('reason', 'conflicting opening or closure information'))
        google = entry.get('google_conflict') or {}
        if google:
            from google_listings import material_identity
            if google.get('anchor') == material_identity(row, entry) and google != (entry.get('acknowledged_google_conflict') or {}).get('snapshot'):
                reasons.append(f"Google {google['kind']} conflict: {google['detail']}")
            else:
                google = {}
        active = bool(reasons)
        if not active and not trading_acknowledged and identifier not in records:
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
        if google:
            records[identifier]['google_conflict'] = dict(google)
            if not records[identifier]['manual_action_required']:
                records[identifier].update(manual_action_required=True, review_category='location_check',
                                           next_action=google['detail'])
        if trading:
            records[identifier]['trading_status_review'] = copy.deepcopy(trading)
        if trading_acknowledged and acknowledged.get('google_finding'):
            records[identifier]['acknowledged_google_trading_finding'] = copy.deepcopy(acknowledged['google_finding'])
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
    browser = False
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
            browser = True
            path = path.parent / 'hermes-google-status.json'
    if not path.exists():
        return None  # Older checkouts have no Google snapshot yet.
    status = json.loads(path.read_text(encoding='utf-8'))
    if status is None:
        raise ValueError('Invalid Google comparison status: null snapshot')
    if browser and (not isinstance(status, dict) or status.get('provider') != 'hermes-browser'):
        raise ValueError('Invalid Google browser receipt; unexpected provider')
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
    browser = status.get('provider') == 'hermes-browser'
    if browser:
        required_receipt = ('run_id', 'source_commit', 'source_release_id', 'validated_release_id',
                            'source_status_sha256', 'execution_state', 'source_execution_finished_at',
                            'checked_at', 'batch')
        if any(key not in status for key in required_receipt):
            raise ValueError('Invalid Google browser receipt; rerun the local comparison')
        if not isinstance(status['run_id'], str) or not status['run_id']:
            raise ValueError('Invalid Google browser receipt; missing run id')
        if status['execution_state'] not in ('complete', 'blocked', 'failed'):
            raise ValueError('Invalid Google browser receipt; invalid execution state')
        for key, pattern in (('source_commit', r'[0-9a-f]{40}'),
                             ('source_release_id', r'[0-9a-f]{64}'),
                             ('source_status_sha256', r'[0-9a-f]{64}')):
            value = status[key]
            if value is not None and (not isinstance(value, str) or not re.fullmatch(pattern, value)):
                raise ValueError('Invalid Google browser receipt; malformed source identity')
        if status['execution_state'] == 'complete' and any(status[key] is None for key in
                ('source_commit', 'source_release_id', 'source_status_sha256')):
            raise ValueError('Invalid Google browser receipt; complete result lacks source identity')
        if not isinstance(status['validated_release_id'], str) or not status['validated_release_id']:
            raise ValueError('Invalid Google browser receipt; missing validated release')
        if not re.fullmatch(r'[0-9a-f]{64}', status['validated_release_id']):
            raise ValueError('Invalid Google browser receipt; malformed validated release')
        if not isinstance(status['batch'], dict):
            raise ValueError('Invalid Google browser receipt; malformed batch')
    directory_signature = status.get('directory_binding_signature')
    if (not isinstance(directory_signature, str) or not re.fullmatch(r'[0-9a-f]{64}', directory_signature)):
        if browser or directory_signature is not None or not status.get('hard_failure'):
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
    diagnostic_only = browser and (status['execution_state'] != 'complete' or status.get('hard_failure', False))
    if diagnostic_only:
        issues.add(GOOGLE_COVERAGE_ISSUE)
        issues.add(GOOGLE_STALE_STATUS_ISSUE)
    for finding in ([] if diagnostic_only else status['findings']):
        row_and_signature = current.get(finding['venue_id'])
        if row_and_signature is None or finding['binding_signature'] != row_and_signature[1]:
            issues.add(GOOGLE_COVERAGE_ISSUE)
            continue
        acknowledged = report.get('records', {}).get(finding['venue_id'], {}).get('acknowledged_google_trading_finding')
        if (acknowledged and google_finding_assessment(finding)['review_category'] == 'trading_check'
                and acknowledged == {key: finding.get(key) for key in ('binding_signature', 'status', 'reason', 'business_status')}):
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
    if browser:
        try:
            finished = datetime.fromisoformat(status['source_execution_finished_at'].replace('Z', '+00:00'))
            age = (now or datetime.now(timezone.utc)) - finished
            stale = (stale or finished.utcoffset() != timedelta(0) or checked_at.utcoffset() != timedelta(0)
                     or not timedelta(0) <= age <= timedelta(hours=48)
                     or checked_at > finished + timedelta(minutes=5))
        except (TypeError, ValueError, AttributeError):
            stale = True
    if stale:
        issues.update((GOOGLE_STALE_STATUS_ISSUE, GOOGLE_COVERAGE_ISSUE))
    if browser and current_bundle is not None:
        # The projection is tied to exact venue bindings. Bundle-only metadata
        # can change its release hash without invalidating those observations;
        # only an exact binding mismatch invalidates projected findings.
        if current_signature != directory_signature:
            issues.update((GOOGLE_COVERAGE_ISSUE, GOOGLE_STALE_STATUS_ISSUE))
            findings = []
    if stale:
        findings = []
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
    elif stale or diagnostic_only:
        total = combined['google_counts'].get('total', 0)
        combined['google_counts'].update(current=0, unchecked_or_stale=total)
        if 'attempted_venue_count' in combined['google_counts']:
            combined['google_counts']['attempted_venue_count'] = 0
    combined['google_provider'] = status.get('provider', 'places-api')
    combined['google_hard_failure'] = status.get('hard_failure', False)
    if browser:
        combined['google_execution_state'] = status['execution_state']
        combined['google_counts']['review_count'] = len(findings)
    for field in ('run_id', 'run_attempt', 'checked_at', 'run_url'):
        combined['google_' + field] = status.get(field, '')
    return combined


def fingerprint(report, pipeline_failure=False):
    """Fingerprint only current owner decisions, not audit/provider state.

    ``pipeline_failure`` remains accepted for old callers, but failures are
    surfaced by the import workflow's dedicated rejected-import issue.
    """
    del pipeline_failure
    return hashlib.sha256(json.dumps(notification_items(report), sort_keys=True).encode()).hexdigest()


def legacy_notification_fingerprint(report):
    """Rebuild the former aggregate email digest to migrate old acknowledgements."""
    active = {identifier: {key: record.get(key) for key in (
        'pub_name', 'place_name', 'postcode', 'status', 'reasons', 'review_category', 'manual_action_required')}
        for identifier, record in report.get('records', {}).items() if record.get('active')}
    for identifier, record in report.get('records', {}).items():
        if identifier not in active:
            continue
        research = record.get('research', {})
        if research.get('proposals'):
            active[identifier]['proposals'] = [{key: proposal.get(key) for key in (
                'name', 'postcode', 'address', 'url', 'lat', 'lng')}
                for proposal in research['proposals']]
        if research.get('status') == 'unavailable':
            active[identifier]['research_error'] = research.get('error')
    if report.get('health_issues'):
        active['map_health'] = sorted(set(report['health_issues']))
    if report.get('google_issues') or report.get('google_findings'):
        active['google_comparison'] = dict(
            issues=sorted(set(report.get('google_issues', []))),
            findings=sorted(report.get('google_findings', []), key=lambda finding: json.dumps(finding, sort_keys=True)))
    if report.get('trading_notice_reviews'):
        active['trading_notice_reviews'] = report['trading_notice_reviews']
    return hashlib.sha256(json.dumps(active, sort_keys=True).encode()).hexdigest()


def notification_items(report):
    """Return current actionable venue questions and due public notices only."""
    items = {}
    for identifier, record in report.get('records', {}).items():
        if not record.get('active') or not record.get('manual_action_required', False):
            continue
        category = record.get('review_category', 'location_check')
        if category == 'trading_check':
            question = f"Can you confirm the current trading/Bass status for {record.get('pub_name', 'this venue')}?"
        elif category == 'identity_check':
            question = f"Can you confirm this is still the correct pub listing for {record.get('pub_name', 'this venue')}?"
        elif category == 'address_metadata':
            question = f"Can you confirm the correct address and postcode for {record.get('pub_name', 'this venue')}?"
        else:
            question = f"Can you verify the mapped entrance or site for {record.get('pub_name', 'this venue')}?"
        item = {key: record.get(key, '') for key in ('pub_name', 'place_name', 'postcode', 'review_category')}
        item['question'] = question
        item['context'] = record.get('next_action') or 'Review the current listing evidence.'
        reference = record.get('reference') or {}
        if category == 'address_metadata' and reference.get('listed') and reference.get('reference'):
            item['context'] = f"The directory gives {reference['listed']}. The venue reference gives {reference['reference']}."
            if reference.get('source_url'):
                item['context'] += ' ' + reference['source_url']
        elif category == 'trading_check':
            reason = next((r.removeprefix('Trading status needs review: ') for r in record.get('reasons', [])
                           if r.startswith('Trading status needs review: ')), '')
            if reason:
                item['context'] = reason
        google = record.get('google_conflict')
        if google:
            item['context'] += f" {google['detail']} Google address: {google['address']}. Business: {google['maps_url']} Directory location: {google['pin_url']}"
        items['venue:' + identifier] = item
    for review in report.get('trading_notice_reviews', []):
        identifier = str(review.get('venue_id', ''))
        if not identifier:
            continue
        items['notice:' + identifier] = {
            'pub_name': review.get('pub_name', ''),
            'place_name': review.get('place_name', ''),
            'postcode': '',
            'review_category': 'trading_notice',
            'question': f"Can you confirm whether the public trading notice for {review.get('pub_name', 'this venue')} is still accurate?",
            'context': f"The notice review date was {review.get('review_by', 'not recorded')}; update or withdraw it after checking current trading evidence.",
        }
        if review.get('text'):
            context = 'The public notice said “' + review['text'] + '”'
            if review.get('source_url'):
                context += ' ' + review['source_url']
            items['notice:' + identifier]['context'] = context
    return items


def format_notification(items):
    """Render only newly changed owner questions; omit diagnostics and history."""
    lines = ['These changed items need an owner decision. Reply in the coordinate review issue after checking the current evidence.', '']
    ordered = sorted(items.items(), key=lambda pair: (pair[1].get('pub_name', '').casefold(), pair[0]))
    for key, item in ordered[:8]:
        location = ' — '.join(part for part in (item.get('place_name'), item.get('postcode')) if part)
        title = item.get('pub_name') or key
        lines += [f"## {title}" + (f" — {location}" if location else ''), '',
                  'Question: ' + item['question'], '', item['context'], '']
    if len(ordered) > 8:
        lines += [f'{len(ordered) - 8} further changed questions are in the review queue and the workflow report.', '']
    lines.append('Review queue: https://github.com/bristolbagpiper/BassBeerMap/issues/5')
    return '\n'.join(lines)


def with_trading_notice_reviews(report, notices, bundle, today=None):
    """Request owner review when a dated public notice reaches its review date."""
    today = today or datetime.now(timezone.utc).date()
    rows = {row['venue_id']: row for row in bundle['rows']}
    reviews = []
    hidden = set(notices.get('manual_hides', []))
    for notice in notices.get('notices', []):
        if notice['venue_id'] in hidden:
            continue
        row = rows.get(notice['venue_id'])
        if row and notice.get('review_by') and date.fromisoformat(notice['review_by']) < today:
            reviews.append({**{field: row[field] for field in ('venue_id', 'pub_name', 'place_name')},
                            'review_by': notice['review_by'],
                            **{key: notice[key] for key in ('text', 'source_url') if notice.get(key)}})
    return dict(report, trading_notice_reviews=sorted(reviews, key=lambda review: review['venue_id']))


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
    if report.get('trading_notice_reviews'):
        lines += ['## Public trading notices due for review', '']
        for review in report['trading_notice_reviews']:
            lines.append(f"- {review['pub_name']} — {review['place_name']} ({review['venue_id']}); review due {review['review_by']}.")
        lines += ['Check current trading evidence, then update or withdraw the dated notice.',
                  'Notices: https://github.com/bristolbagpiper/BassBeerMap/blob/main/trading-notices.json', '']
    def render_record(identifier, record):
        lines.extend([f"{record['pub_name']} — {record['place_name']} ({record['postcode']})",
                      f"ID: {identifier} | First seen: {record['first_seen']} | Last checked: {record.get('last_checked') or 'not checked'}",
                      *[f"- {reason}" for reason in record['reasons']]])
        lines.append(f"- Next action: {record.get('next_action', classify_review(record)['next_action'])}")
        if record.get('manual_review_request'):
            lines.append('- Manual comparison source: ' + record['manual_review_request']['source_url'])
        if conflict := record.get('google_conflict'):
            lines.append(f"- Google business: {conflict['address']} {conflict['maps_url']} | Directory location: {conflict['pin_url']}")
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

