"""Deliver changed location alerts; acknowledge delivery only after SMTP succeeds."""
import argparse
import json
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from directory_release import read_json, write_json
from location_review import fingerprint, format_report, load_google_status, load_health_status, with_google, with_health


def notify(report, state_path, failure_url='', smtp_factory=smtplib.SMTP):
    if os.environ.get('SOURCE_CONCLUSION') == 'cancelled':
        failure_url = ''  # A deliberately/superseded canceled run is not a failed import.
    state = read_json(state_path, {})
    digest = fingerprint(report)
    if not failure_url and (state.get('email_fingerprint') == digest or (
            not report['active_count'] and not report.get('health_count') and not report.get('google_count') and not state)):
        print('No changed actionable location report; email remains quiet.')
        return False
    required = ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'ALERT_TO')
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError('Location alerts cannot be delivered: missing ' + ', '.join(missing))
    message = EmailMessage()
    actionable_count = report.get('actionable_count', report['active_count'])
    source_warning_count = report.get('source_warning_count', 0)
    message['Subject'] = f"BassBeerMap: {report['unverified_count']} unverified locations, {actionable_count} actions"
    if source_warning_count:
        message.replace_header('Subject', message['Subject'] + f", {source_warning_count} source warnings")
    if report.get('health_count'):
        message.replace_header('Subject', message['Subject'] + f", {report['health_count']} map health checks")
    google_actionable_count = report.get('google_actionable_count')
    if google_actionable_count is None:
        from owner_location_review import google_finding_assessment
        google_actionable_count = sum(google_finding_assessment(finding)['manual_action_required']
                                      for finding in report.get('google_findings', []))
    if google_actionable_count:
        message.replace_header('Subject', message['Subject'] + f", {google_actionable_count} Google pub actions")
    google_source_warning_count = report.get('google_source_warning_count')
    if google_source_warning_count is None:
        from owner_location_review import google_finding_assessment
        google_source_warning_count = sum(not google_finding_assessment(finding)['manual_action_required']
                                          for finding in report.get('google_findings', []))
    if google_source_warning_count:
        message.replace_header('Subject', message['Subject'] + f", {google_source_warning_count} Google source warnings")
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
    parser.add_argument('--health-status', default='map-health-status.json')
    parser.add_argument('--google-status', default='google-places-status.json')
    parser.add_argument('--directory-release', default='directory-release.json')
    args = parser.parse_args()
    report = with_google(with_health(read_json(args.report), load_health_status(args.health_status)),
                         load_google_status(args.google_status), read_json(args.directory_release))
    notify(report, Path(args.state), args.failure_url)
