"""Deliver only changed owner questions; record SMTP acceptance per item."""
import argparse
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from directory_release import read_json, write_json
from location_review import format_notification, legacy_notification_fingerprint, notification_items


def set_workflow_result(value):
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write(f'result={value}\n')


def acknowledgement_state(previous_state, items, accepted=False):
    """Build per-item state while retaining the receipt for the legacy email."""
    legacy = previous_state.get('legacy_ack') if isinstance(previous_state, dict) else None
    if not legacy and isinstance(previous_state, dict) and previous_state.get('email_fingerprint'):
        legacy = {
            'email_fingerprint': previous_state['email_fingerprint'],
            'emailed_at': previous_state.get('emailed_at'),
        }
    result = {'schema_version': 2, 'items': items}
    if legacy:
        result['legacy_ack'] = legacy
    if accepted:
        result['emailed_at'] = datetime.now(timezone.utc).isoformat()
    elif isinstance(previous_state, dict) and previous_state.get('emailed_at'):
        result['emailed_at'] = previous_state['emailed_at']
    return result


def notify(report, state_path, failure_url='', smtp_factory=smtplib.SMTP):
    """Send changed decisions once; a pipeline failure is handled by its source workflow."""
    del failure_url  # The import workflow owns rejected-import alerts and recovery.
    state = read_json(state_path, {})
    previous = state.get('items', {}) if isinstance(state, dict) else {}
    current = notification_items(report)
    if (not previous and state.get('email_fingerprint')
            and state['email_fingerprint'] == legacy_notification_fingerprint(report)):
        # The prior aggregate acknowledgement covered this exact report. Migrate
        # it once so existing unresolved questions do not all re-email at deploy.
        write_json(state_path, acknowledgement_state(state, current))
        set_workflow_result('quiet')
        print('Legacy acknowledgement matches the current report; migrated quietly.')
        return False
    changed = {key: item for key, item in current.items()
               if previous.get(key) != item}

    if not changed:
        # Prune cleared decisions even when no email is needed, so a later
        # recurrence is a new question. This also migrates the old aggregate state.
        if previous != current or state.get('schema_version') != 2:
            write_json(state_path, acknowledgement_state(state, current))
        set_workflow_result('quiet')
        print('No changed actionable owner question; email remains quiet.')
        return False

    required = ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'ALERT_TO')
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError('Location alerts cannot be delivered: missing ' + ', '.join(missing))

    message = EmailMessage()
    count = len(changed)
    message['Subject'] = f"BassBeerMap: {count} changed review question{'s' if count != 1 else ''}"
    message['From'] = os.environ.get('ALERT_FROM') or os.environ['SMTP_USERNAME']
    message['To'] = os.environ['ALERT_TO']
    message.set_content(format_notification(changed))

    accepted = False
    try:
        with smtp_factory(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT') or '587'), timeout=30) as server:
            server.starttls()
            server.login(os.environ['SMTP_USERNAME'], os.environ['SMTP_PASSWORD'])
            refused = server.send_message(message)
            if refused:
                raise RuntimeError('SMTP refused an alert recipient; delivery has not been acknowledged')
            accepted = True
    except OSError:
        if not accepted:
            raise
        print('The SMTP connection closed after accepting the alert.')

    write_json(state_path, acknowledgement_state(state, current, accepted=True))
    set_workflow_result('accepted')
    print('Changed owner questions accepted by configured SMTP server; inbox delivery is not confirmed.')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', default='location-review.json')
    parser.add_argument('--state', default='location-alert-state.json')
    parser.add_argument('--failure-url', default='')
    args = parser.parse_args()
    notify(read_json(args.report), Path(args.state), args.failure_url)
