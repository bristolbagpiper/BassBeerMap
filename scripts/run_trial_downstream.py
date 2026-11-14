#!/usr/bin/env python3
"""Build a local Pages-shaped artifact and run the real notifier with fake SMTP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from email.message import EmailMessage
from pathlib import Path
from urllib.request import urlopen


class FakeSMTP:
    """In-memory SMTP adapter: it never opens a socket or contacts a mail host."""

    messages = []

    def __init__(self, host, port, timeout=30):
        if not str(host).endswith('.invalid') or int(port) != 2525:
            raise ValueError('Trial notifier must use the reserved .invalid host and fake port')

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def starttls(self):
        return None

    def login(self, username, password):
        if not username.endswith('.invalid') or password != 'trial-only-placeholder':
            raise ValueError('Trial notifier received unexpected credentials')

    def send_message(self, message: EmailMessage):
        payload = message.as_bytes()
        self.messages.append({
            'sha256': hashlib.sha256(payload).hexdigest(),
            'subject': str(message.get('Subject', '')),
            'to': str(message.get('To', '')),
        })
        return {}


def run(repo_root, output, commit):
    repo_root, output = Path(repo_root).resolve(), Path(output).resolve()
    sys.path.insert(0, str(repo_root))
    from directory_release import read_json, write_json
    from location_review import (load_google_status, load_health_status, with_google, with_health)
    from notify_location_review import notify

    bundle = read_json(repo_root / 'directory-release.json')
    report = read_json(repo_root / 'location-review.json')
    report = with_health(report, load_health_status(repo_root / 'map-health-status.json'))
    report = with_google(report, load_google_status(repo_root / 'google-places-status.json'), bundle)

    output.mkdir(parents=True, exist_ok=True)
    FakeSMTP.messages.clear()
    trial_env = {
        'SMTP_HOST': 'memory.protection-trial.invalid',
        'SMTP_PORT': '2525',
        'SMTP_USERNAME': 'protection-trial@example.invalid',
        'SMTP_PASSWORD': 'trial-only-placeholder',
        'ALERT_TO': 'protection-trial@example.invalid',
        'ALERT_FROM': 'protection-trial@example.invalid',
    }
    prior = {'email_fingerprint': '0' * 64}
    with tempfile.TemporaryDirectory(prefix='bass-protection-trial-') as temp:
        state_path = Path(temp) / 'location-alert-state.json'
        write_json(state_path, prior)
        previous_env = {key: os.environ.get(key) for key in trial_env}
        try:
            os.environ.update(trial_env)
            delivered = notify(report, state_path, smtp_factory=FakeSMTP)
            state = read_json(state_path)
            suppressed = not notify(report, state_path, smtp_factory=FakeSMTP)
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if not delivered or not suppressed or len(FakeSMTP.messages) != 1:
            raise RuntimeError('Fake SMTP notification did not send once and suppress its exact replay')
        write_json(output / 'location-alert-state.json', state)

    assets = ('index.html', 'directory-release.json', 'hermes-google-status.json',
              'trading-notices.json', 'pubs.csv', 'latest-bass-directory.pdf',
              'bass-logo.svg', 'credit-link-icon.png')
    for name in assets:
        shutil.copyfile(repo_root / name, output / name)
    release_id = bundle.get('release_id')
    if not isinstance(release_id, str) or len(release_id) != 64:
        raise ValueError('Validated directory release has no SHA-256 release ID')
    handler = partial(SimpleHTTPRequestHandler, directory=output)
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for name in assets:
                url = f'http://127.0.0.1:{server.server_port}/{name}'
                with urlopen(url, timeout=10) as response:
                    if response.read() != (repo_root / name).read_bytes():
                        raise RuntimeError(f'Staged map asset does not match the merge: {name}')
        finally:
            server.shutdown()
            thread.join()
    manifest = {
        'schema_version': 1,
        'environment': 'protection-trial',
        'source_commit': commit,
        'release_id': release_id,
        'hermes_run_id': read_json(repo_root / 'hermes-google-status.json')['run_id'],
        'deployed_asset_count': len(assets),
        'notification': {
            'transport': 'fake-smtp-no-network',
            'accepted': True,
            'replay_suppressed': True,
            'message_sha256': FakeSMTP.messages[0]['sha256'],
            'subject': FakeSMTP.messages[0]['subject'],
            'recipient': FakeSMTP.messages[0]['to'],
            'state_fingerprint': state['email_fingerprint'],
        },
    }
    write_json(output / 'trial-manifest.json', manifest)
    if json.loads((output / 'directory-release.json').read_text(encoding='utf-8')) != bundle:
        raise RuntimeError('Deployment artifact directory release did not survive the build')
    if not (output / 'index.html').is_file():
        raise RuntimeError('Deployment artifact has no index.html')
    print(json.dumps(manifest, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args(argv)
    run(args.repo_root, args.output, args.commit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
