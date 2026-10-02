"""Deliver changed location alerts; acknowledge delivery only after SMTP succeeds."""
import argparse
import json
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from directory_release import read_json, write_json
from location_review import fingerprint, format_report


def notify(report, state_path, failure_url='', smtp_factory=smtplib.SMTP):
    if os.environ.get('SOURCE_CONCLUSION') == 'cancelled':
        failure_url = ''  # A deliberately/superseded canceled run is not a failed import.
    state = read_json(state_path, {})
    digest = fingerprint(report)
    if not failure_url and (state.get('email_fingerprint') == digest or (not report['active_count'] and not state)):
        print('No changed actionable location report; email remains quiet.')
        return False
    required = ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'ALERT_TO')
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError('Location alerts cannot be delivered: missing ' + ', '.join(missing))
    message = EmailMessage()
    message['Subject'] = f"BassBeerMap: {report['unverified_count']} unverified locations, {report['active_count']} reviews"
    message['From'] = os.environ.get('ALERT_FROM') or os.environ['SMTP_USERNAME']
    message['To'] = os.environ['ALERT_TO']
    body = format_report(report)
    if failure_url:
        body = 'A directory import or weekly audit failed. Inspect its validation report:\n' + failure_url + '\n\n' + body
    message.set_content(body)
    with smtp_factory(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT') or '587'), timeout=30) as server:
        server.starttls()
        server.login(os.environ['SMTP_USERNAME'], os.environ['SMTP_PASSWORD'])
        refused = server.send_message(message)
        if refused:
            raise RuntimeError('SMTP refused an alert recipient; delivery has not been acknowledged')
    write_json(state_path, dict(email_fingerprint=digest, emailed_at=datetime.now(timezone.utc).isoformat()))
    print('Location alert accepted by configured SMTP server; delivery recorded.')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', default='location-review.json')
    parser.add_argument('--state', default='location-alert-state.json')
    parser.add_argument('--failure-url', default='')
    args = parser.parse_args()
    notify(read_json(args.report), Path(args.state), args.failure_url)
