"""Send changed Google review findings privately through the existing mail account."""

import json
import subprocess
import tomllib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path

from build_place_review_queue import digest
from directory_release import read_json
from owner_location_review import google_finding_assessment


def _mail(executable, config, account, command, backend, run, content=None):
    # Himalaya splits config paths on colons, including Windows drive letters.
    return run(
        [executable, *command, '--config', config.name, '--account', account,
         '--backend', backend, '--json'],
        cwd=config.parent, input=content, capture_output=True, timeout=45, check=False,
    )


def _inbox_has_reference(executable, config, account, reference, run):
    result = run(
        [executable, 'envelope', 'search', '--config', config.name, '--account', account,
         '--backend', 'imap', '--json', 'subject', reference],
        cwd=config.parent, capture_output=True, timeout=45, check=False,
    )
    if result.returncode != 0:
        return False
    messages = json.loads(result.stdout)['envelopes']
    return any(reference in message.get('subject', '') for message in messages)


def notify(state_dir, status, execution, save_json, *, run=subprocess.run):
    """Acknowledge only accepted messages and check the inbox before any repeat."""
    path = state_dir / 'notification-state.json'
    settings = read_json(state_dir / 'notification.json')
    if settings is None:
        return {'state': 'not_configured'}
    executable = str(Path(settings['executable']).resolve(strict=True))
    config = Path(settings['config']).resolve(strict=True)
    account = settings['account']
    address = tomllib.loads(config.read_text(encoding='utf-8'))['accounts'][account]['email']
    if not isinstance(address, str) or '@' not in address or any(char in address for char in '\r\n,;'):
        raise ValueError('Mail account must identify a single mailbox')

    journal = read_json(path, {})
    pending = journal.get('pending')
    if pending:
        if not _inbox_has_reference(executable, config, account, pending['reference'], run):
            return {'state': 'unconfirmed', 'reference': pending['reference']}
        journal = {'accepted': {**pending, 'state': 'inbox_verified'}}
        save_json(path, journal)

    findings = [finding for finding in (status or {}).get('findings', [])
                if google_finding_assessment(finding)['manual_action_required']]
    actions = sorted((item['venue_id'], item['binding_signature'], item['reason']) for item in findings)
    issues = sorted(set((status or {}).get('issues', [])))
    if execution['state'] != 'complete':
        issues.append(execution.get('detail', 'Google Maps comparison failed'))
    fingerprint = digest([actions, issues])
    accepted = journal.get('accepted', {})
    if accepted.get('fingerprint') == fingerprint:
        return {'state': 'unchanged', 'reference': accepted['reference']}
    if not actions and not issues and not accepted:
        return {'state': 'unchanged'}

    if _mail(executable, config, account, ('account', 'check'), 'smtp', run).returncode != 0:
        return {'state': 'failed', 'detail': 'Mail account SMTP authentication or connection failed'}
    reference = 'BBM-' + execution['execution_id'][:12]
    message = EmailMessage(policy=SMTP)
    message['From'] = address
    message['To'] = address
    message['Subject'] = f'BassBeerMap Google review · {len(actions)} venue findings · {reference}'
    sent_at = datetime.now(timezone.utc)
    message['Date'] = format_datetime(sent_at)
    message['Message-ID'] = f'<{reference.lower()}@bassbeermap.local>'
    lines = ['BassBeerMap Google Maps comparison', '',
             f"Execution {execution['execution_id']} · {execution['state']}",
             f"Checked in this run: {(status or {}).get('counts', {}).get('checked_this_run', 0)}"]
    lines.extend(f'- {issue}' for issue in issues)
    lines.extend(f"- {finding['pub_name']}, {finding['place_name']} ({finding['postcode']}): {finding['reason']}"
                 for finding in findings)
    lines.extend(('', 'Treat Google findings as review prompts. Confirm them against the venue or its directory evidence.'))
    message.set_content('\n'.join(lines))
    pending = {'fingerprint': fingerprint, 'reference': reference, 'execution_id': execution['execution_id'],
               'submitted_at': sent_at.isoformat()}
    save_json(path, {**journal, 'pending': pending})
    try:
        result = _mail(executable, config, account, ('message', 'send'), 'smtp', run, message.as_bytes())
    except (subprocess.TimeoutExpired, OSError):
        return {'state': 'unconfirmed', 'reference': reference}
    if result.returncode != 0:
        return {'state': 'unconfirmed', 'reference': reference}
    save_json(path, {'accepted': {**pending, 'state': 'smtp_accepted',
                                  'accepted_at': datetime.now(timezone.utc).isoformat()}})
    return {'state': 'smtp_accepted', 'reference': reference}
