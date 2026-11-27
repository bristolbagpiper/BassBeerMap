"""Deliver changed owner questions with durable per-item reservations."""
import argparse
import base64
import hashlib
import json
import os
import re
import smtplib
import subprocess
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from directory_release import read_json, write_json
from location_review import (format_notification, format_report, legacy_notification_fingerprint,
                             notification_items)


STATE_MARKER = re.compile(r'<!-- bassbeermap-notification-state:v([12]):([A-Za-z0-9_-]+) -->')
UNCERTAIN_RESOLUTIONS = {'none', 'confirm_sent', 'confirm_not_sent'}
MAX_ISSUE_BODY_CHARS = 64000


def item_fingerprint(item):
    # 96 bits distinguish individual notification contents at directory scale.
    if isinstance(item, str) and re.fullmatch(r'h:[A-Za-z0-9_-]{16}', item):
        return item
    if isinstance(item, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', item):
        digest = bytes.fromhex(item[7:])
    else:
        encoded = json.dumps(item, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
        digest = hashlib.sha256(encoded).digest()
    return 'h:' + base64.urlsafe_b64encode(digest[:12]).decode('ascii').rstrip('=')


def set_workflow_result(value):
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write(f'result={value}\n')


def normalize_state(state):
    state = dict(state) if isinstance(state, dict) else {}
    legacy = state.get('legacy_ack')
    if not legacy and state.get('email_fingerprint'):
        legacy = {'email_fingerprint': state['email_fingerprint'],
                  'emailed_at': state.get('emailed_at')}
    old_items = state.get('items', {}) if isinstance(state.get('items', {}), dict) else {}
    items = {key: item_fingerprint(value) for key, value in old_items.items()}
    old_pending = state.get('pending', {}) if isinstance(state.get('pending', {}), dict) else {}
    pending = {}
    for reservation_id, reservation in old_pending.items():
        if not isinstance(reservation, dict):
            continue
        key = reservation.get('key') or reservation_id
        entry = {'status': reservation.get('status', 'reserved'),
                 'reserved_at': reservation.get('reserved_at', ''), 'run_id': reservation.get('run_id', '')}
        if 'item' in reservation:
            entry['item_hash'] = item_fingerprint(reservation['item'])
        elif reservation.get('item_hash'):
            entry['item_hash'] = item_fingerprint(reservation['item_hash'])
        pending[key] = entry
    result = {'schema_version': 4, 'items': items, 'pending': pending}
    if legacy:
        result['legacy_ack'] = legacy
    if state.get('emailed_at'):
        result['emailed_at'] = state['emailed_at']
    return result


def encode_state(state):
    raw = json.dumps(state, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode('ascii').rstrip('=')


def decode_state(body):
    match = STATE_MARKER.search(body or '')
    if not match:
        return None
    version, token = match.groups()
    token += '=' * (-len(token) % 4)
    raw = base64.urlsafe_b64decode(token)
    if version == '1':
        value = json.loads(raw.decode('utf-8'))
    else:
        value = json.loads(zlib.decompress(raw).decode('utf-8'))
    if not isinstance(value, dict) or value.get('schema_version') not in (3, 4):
        raise ValueError('Issue #5 notification state is malformed or unsupported.')
    return normalize_state(value)


def issue_body(report_text, state):
    state = normalize_state(state)
    pending = state.get('pending', {})
    report = report_text.rstrip()
    if pending:
        uncertain = sum(value.get('status') == 'uncertain' for value in pending.values())
        reserved = len(pending) - uncertain
        status = ['## Notification delivery status', '']
        if uncertain:
            status.append(f'{uncertain} item(s) have an uncertain SMTP outcome. Automatic resend is suppressed.')
        if reserved:
            status.append(f'{reserved} item(s) were reserved before a notification attempt; automatic resend is suppressed.')
        status += ['', 'After checking the mail outcome, manually dispatch the notification workflow with '
                   '`confirm_sent` or `confirm_not_sent` to resolve the held item(s).']
        status_text = '\n\n' + '\n'.join(status)
    else:
        status_text = ''
    marker = '\n\n<!-- bassbeermap-notification-state:v2:' + encode_state(state) + ' -->'
    available = MAX_ISSUE_BODY_CHARS - len(status_text) - len(marker)
    if available < 0:
        raise RuntimeError('Notification receipt state exceeds the safe GitHub issue-body limit.')
    if len(report) > available:
        suffix = '\n\n_Report shortened to preserve the complete notification receipt state._'
        report = (report[:available - len(suffix)].rstrip() + suffix) if available >= len(suffix) else ''
    body = report + status_text + marker
    if len(body) > MAX_ISSUE_BODY_CHARS:
        raise RuntimeError('Notification issue body exceeds the safe GitHub issue-body limit.')
    return body


class GitHubIssueStateStore:
    """Persist reservations and acknowledgements in the existing review issue."""

    def __init__(self, repository, token, issue_number=5, opener=urllib.request.urlopen):
        if not repository or not token:
            raise RuntimeError('GitHub issue state requires repository and workflow-token context.')
        self.repository = repository
        self.token = token
        self.issue_number = int(issue_number)
        self.opener = opener
        self.base_url = f'https://api.github.com/repos/{repository}/issues/{self.issue_number}'

    def _request(self, method, payload=None):
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        request = urllib.request.Request(
            self.base_url, data=data, method=method,
            headers={'Accept':'application/vnd.github+json', 'Authorization':f'Bearer {self.token}',
                     'X-GitHub-Api-Version':'2022-11-28', 'User-Agent':'BassBeerMap-notification-state',
                     'Content-Type':'application/json'})
        try:
            with self.opener(request, timeout=20) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'GitHub issue state request failed with HTTP {error.code}.') from error

    def load(self, seed):
        issue = self._request('GET')
        body = issue.get('body') or ''
        stored = decode_state(body)
        return normalize_state(seed if stored is None else stored)

    def save(self, report_text, state, should_open):
        # Read again immediately before the update so an unrelated issue edit is
        # not silently overwritten with a stale body snapshot.
        latest = self._request('GET')
        rendered = issue_body(report_text, state)
        desired_state = 'open' if should_open else 'closed'
        if latest.get('body') != rendered or latest.get('state') != desired_state:
            saved = self._request('PATCH', {'body':rendered, 'state':desired_state})
        else:
            saved = latest
        if decode_state(saved.get('body') or '') != normalize_state(state):
            raise RuntimeError('GitHub did not persist the notification receipt state on issue #5.')
        return saved


def _read_state(state_path, state_store):
    seed = read_json(state_path, {}) if state_path else {}
    if state_store:
        return normalize_state(state_store.load(seed))
    return normalize_state(seed)


def _persist(state, report, state_path, state_store):
    state = normalize_state(state)
    if state_store:
        due_notices = report.get('trading_notice_reviews', [])
        current = notification_items(report)
        state_store.save(format_report(report), state,
                         bool(current or due_notices or state.get('pending')))
    else:
        if not state_path:
            raise RuntimeError('Notification state path is required for local state storage.')
        write_json(state_path, state)


def _resolve_pending(state, current, resolution):
    if resolution not in UNCERTAIN_RESOLUTIONS:
        raise ValueError(f'Unknown notification resolution: {resolution}')
    if resolution == 'none':
        return 0
    pending = state.get('pending', {})
    resolved = len(pending)
    if resolution == 'confirm_sent':
        for key, reservation in pending.items():
            if key.startswith('pdf:') or (key in current and reservation.get('item_hash') == item_fingerprint(current[key])):
                state['items'][key] = reservation['item_hash']
    state['pending'] = {}
    return resolved


def _message_for(changed, smtp_env, publication=None, questions=None):
    message = EmailMessage()
    count = len(changed)
    message['From'] = smtp_env.get('ALERT_FROM') or smtp_env['SMTP_USERNAME']
    message['To'] = smtp_env['ALERT_TO']
    if publication is None:
        message['Subject'] = f"BassBeerMap: {count} changed review question{'s' if count != 1 else ''}"
        message.set_content(format_notification(changed))
    else:
        from send_change_report_email import format_publication
        message['Subject'] = f"BassBeerMap: {publication['edition']} published"
        message.set_content(format_publication(publication, questions))
    return message


def notify(report, state_path=None, failure_url='', smtp_factory=smtplib.SMTP,
           state_store=None, resolution='none', environ=None, publication=None):
    """Send changed questions once; uncertain sends are held for human resolution."""
    del failure_url  # Rejected imports have their own source workflow issue.
    environ = os.environ if environ is None else environ
    state = _read_state(state_path, state_store)
    questions = notification_items(report)
    current = dict(questions)
    release_key = None
    if publication is not None:
        revision = publication.get('pdf_revision', publication['commit'])
        release_key = 'pdf:' + revision
        current[release_key] = revision
    state['items'] = {key: item for key, item in state['items'].items() if key in current or key.startswith('pdf:')}

    resolved = _resolve_pending(state, current, resolution)
    if publication is None and not state.get('items') and not state.get('pending') and state.get('legacy_ack'):
        legacy_hash = state['legacy_ack'].get('email_fingerprint')
        if legacy_hash and legacy_hash == legacy_notification_fingerprint(report):
            state['items'] = {key: item_fingerprint(item) for key, item in current.items()}
            _persist(state, report, state_path, state_store)
            set_workflow_result('quiet')
            print('Exact legacy acknowledgement migrated into per-item issue state.')
            return False

    changed = {key:item for key,item in current.items()
               if state['items'].get(key) != item_fingerprint(item) and key not in state['pending']}
    if not changed:
        _persist(state, report, state_path, state_store)
        if state.get('pending'):
            set_workflow_result('uncertain')
            print('Held notification reservation remains unresolved; no email was attempted.')
        elif resolved:
            set_workflow_result('resolved_sent' if resolution == 'confirm_sent' else 'resolved_not_sent')
            print(f'Resolved {resolved} held notification item(s) by explicit manual choice.')
        else:
            set_workflow_result('quiet')
            print('No changed actionable owner question; email remains quiet.')
        return False

    required = ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'ALERT_TO')
    missing = [key for key in required if not environ.get(key)]
    if missing:
        if state_store:
            _persist(state, report, state_path, state_store)
        raise RuntimeError('Location alerts cannot be delivered: missing ' + ', '.join(missing))

    smtp_env = {key:environ[key] for key in required}
    smtp_env['SMTP_PORT'] = environ.get('SMTP_PORT') or '587'
    smtp_env['ALERT_FROM'] = environ.get('ALERT_FROM')
    message = _message_for(changed, smtp_env, publication if release_key in changed else None, questions)
    reservation_ids = []
    now = datetime.now(timezone.utc).isoformat()
    run_id = environ.get('GITHUB_RUN_ID', '')
    for key, item in changed.items():
        # The prior question for this venue has changed. Its receipt no longer
        # represents the current question, which now has its own reservation.
        state['items'].pop(key, None)
        state['pending'][key] = {'item_hash':item_fingerprint(item), 'status':'reserved', 'reserved_at':now, 'run_id':run_id}
        reservation_ids.append(key)
    # The issue write is the send boundary: no SMTP operation happens until the
    # reservation has been read back successfully.
    _persist(state, report, state_path, state_store)

    stage = 'pre_send'
    accepted = False
    try:
        with smtp_factory(smtp_env['SMTP_HOST'], int(smtp_env['SMTP_PORT']), timeout=30) as server:
            server.starttls()
            server.login(smtp_env['SMTP_USERNAME'], smtp_env['SMTP_PASSWORD'])
            stage = 'sending'
            refused = server.send_message(message)
            if refused:
                raise RuntimeError('SMTP reported recipient refusal; delivery may be partial, so resend is held for review.')
            accepted = True
    except OSError:
        if accepted:
            print('SMTP connection closed after accepting the notification.')
        elif stage == 'pre_send':
            for reservation_id in reservation_ids:
                state['pending'].pop(reservation_id, None)
            _persist(state, report, state_path, state_store)
            set_workflow_result('failed_pre_send')
            raise
        else:
            for reservation_id in reservation_ids:
                if reservation_id in state['pending']:
                    state['pending'][reservation_id]['status'] = 'uncertain'
            try:
                _persist(state, report, state_path, state_store)
            finally:
                set_workflow_result('uncertain')
            raise RuntimeError('SMTP outcome is uncertain; automatic resend is suppressed until manually resolved.')
    except Exception:
        if stage == 'pre_send':
            for reservation_id in reservation_ids:
                state['pending'].pop(reservation_id, None)
            _persist(state, report, state_path, state_store)
            set_workflow_result('failed_pre_send')
            raise
        if not accepted:
            for reservation_id in reservation_ids:
                if reservation_id in state['pending']:
                    state['pending'][reservation_id]['status'] = 'uncertain'
            try:
                _persist(state, report, state_path, state_store)
            finally:
                set_workflow_result('uncertain')
            raise RuntimeError('SMTP outcome is uncertain; automatic resend is suppressed until manually resolved.')

    for key, item in changed.items():
        state['items'][key] = item_fingerprint(item)
    for reservation_id in reservation_ids:
        state['pending'].pop(reservation_id, None)
    state['emailed_at'] = datetime.now(timezone.utc).isoformat()
    _persist(state, report, state_path, state_store)
    if state.get('pending'):
        set_workflow_result('uncertain')
        print('SMTP accepted new questions; older held reservations still need manual resolution.')
    else:
        set_workflow_result('accepted')
        print('SMTP accepted changed questions; inbox delivery is not confirmed.')
    return True


def read_publication(commit, previous, published_on, repository, token):
    """Bind a dispatched report to exact source bytes and a successful Pages deployment."""
    from build_change_report import published_report
    from request_pages_release import ASSET_PATHS, _check_public_assets, _git_bytes, _json_request
    if not all(re.fullmatch(r'[0-9a-f]{40}', sha) for sha in (commit, previous)):
        raise ValueError('Both publication references must be full Git commit IDs')
    publication_date = date.fromisoformat(published_on).isoformat()
    subprocess.run(['git','merge-base','--is-ancestor',commit,'HEAD'],check=True)
    revision = subprocess.check_output(['git','log','-1','--format=%H',commit,'--','latest-bass-directory.pdf'],
                                       text=True).strip()
    subprocess.run(['git','merge-base','--is-ancestor',previous,revision+'^'],check=True)
    def blob(rev):
        return subprocess.check_output(['git','rev-parse',rev+':latest-bass-directory.pdf']).strip()
    if blob(commit) == blob(previous):
        raise ValueError('Published and previous PDF bytes are identical')
    # The import workflow dispatches from its repo token only after matching
    # the live PDF, CSV, HTML and directory bundle to this exact commit.
    if (os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            and os.environ.get('GITHUB_ACTOR') == 'github-actions[bot]'):
        return dict(published_report(commit, previous, publication_date), pdf_revision=revision)
    # An operator can also replay a publication explicitly. Check an actual
    # successful deployment before treating manual inputs as a release.
    api = f'https://api.github.com/repos/{repository}/deployments'
    for deployment in _json_request(f'{api}?environment=github-pages&sha={commit}', token=token):
        if deployment.get('sha') != commit:
            continue
        statuses = _json_request(f"{api}/{deployment['id']}/statuses",token=token)
        for status in statuses:
            if status['state'] == 'success':
                report = published_report(commit, previous, status['created_at'])
                # A canonical upload can already be on main when its import is
                # validated. Replaying it after an unrelated commit is one delivery.
                report['pdf_revision'] = revision
                return report
    # Some branch-based Pages builds have no Deployments API record.
    build = _json_request(f'https://api.github.com/repos/{repository}/pages/builds/latest', token=token)
    if (build.get('commit'), build.get('status')) == (commit, 'built'):
        expected = {path: _git_bytes(commit, path) for path in ASSET_PATHS}
        matches, _ = _check_public_assets(expected, base_url='https://bassbeermap.com',opener=urllib.request.urlopen)
        if matches:
            return dict(published_report(commit, previous, build['updated_at']), pdf_revision=revision)
    raise ValueError('Cannot verify live publication of the requested commit')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', default='notification-report.json')
    parser.add_argument('--state-seed', default='location-alert-state.json')
    parser.add_argument('--resolution', choices=sorted(UNCERTAIN_RESOLUTIONS), default='none')
    parser.add_argument('--issue', type=int, default=5)
    args = parser.parse_args()
    store = GitHubIssueStateStore(os.environ.get('GITHUB_REPOSITORY', ''),
                                  os.environ.get('GH_TOKEN', ''), args.issue)
    refs = [os.environ.get(k,'') for k in ('PUBLICATION_COMMIT','PREVIOUS_COMMIT','PUBLISHED_ON')]
    publication = read_publication(*refs, store.repository, store.token) if any(refs) else None
    notify(read_json(args.report), Path(args.state_seed), state_store=store,
           resolution=args.resolution, publication=publication)
