"""Local Hermes entry point: fetch published data, browse, and save review status.

This script cannot publish to GitHub or move a pin. The connected publisher
uploads the derived status after checking its execution receipt and bindings.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from urllib.request import Request, urlopen
import uuid

from directory_release import read_json
from hermes_google_browser import run as browse
from hermes_google_review import build_status

REPOSITORY = 'bristolbagpiper/BassBeerMap'
DEFAULT_STATE = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'hermes' / 'profiles' / 'mcjeeves' / 'bass-map-google'


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def public_json(url, max_bytes=12_000_000):
    request = Request(url, headers={'User-Agent': 'BassBeerMap Hermes location review'})
    with urlopen(request, timeout=30) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('Public source exceeds size limit')
    return json.loads(data)


def validate_inputs(bundle, registry):
    payload = {key: value for key, value in bundle.items() if key != 'release_id'}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if bundle.get('schema_version') != 1 or bundle.get('release_id') != digest:
        raise ValueError('Invalid published release')
    identifiers = set()
    for row in bundle['rows']:
        identifier = row['venue_id']
        if identifier in identifiers:
            raise ValueError('Duplicate venue identity')
        identifiers.add(identifier)
        entry = registry['venues'][identifier]
        if any(entry['listing'].get(key) != row.get(key) for key in ('pub_name', 'place_name', 'postcode')):
            raise ValueError('Registry and release identities differ')
        location = bundle['locations'][identifier]
        if location['precision'] == 'venue' and any(entry['verified_pin'][key] != location[key] for key in ('lat', 'lng')):
            raise ValueError('Registry and release points differ')
    if identifiers != set(bundle['locations']):
        raise ValueError('Published location coverage differs')


def fetch_inputs(destination, lookup=public_json):
    reference = lookup(f'https://api.github.com/repos/{REPOSITORY}/git/ref/heads/main')
    commit = reference['object']['sha']
    if len(commit) != 40 or any(char not in '0123456789abcdef' for char in commit):
        raise ValueError('Invalid published commit')
    prefix = f'https://raw.githubusercontent.com/{REPOSITORY}/{commit}/'
    bundle = lookup(prefix + 'directory-release.json')
    registry = lookup(prefix + 'venue-registry.json')
    validate_inputs(bundle, registry)
    atomic_json(destination / 'directory-release.json', bundle)
    atomic_json(destination / 'venue-registry.json', registry)
    return commit


def execute(state_dir=DEFAULT_STATE, limit=45, venue_ids=(), fetch=True, browser_run=browse):
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / 'runner.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    receipt_path = state_dir / 'observations.json'
    execution = dict(schema_version=1, provider='hermes-browser', execution_id=uuid.uuid4().hex,
                     started_at=now(), finished_at=None, state='running')
    try:
        atomic_json(state_dir / 'last-run.json', execution)
        data = state_dir / 'data'
        data.mkdir(exist_ok=True)
        commit = fetch_inputs(data) if fetch else None
        browser_home = state_dir / 'browser-home'
        browser_home.mkdir(exist_ok=True)
        config = browser_home / 'config.yaml'
        expected = ('browser:\n  cloud_provider: local\n  backend: builtin\n  engine: chrome\n'
                    '  headed: false\n  use_real_profile: false\n  record_sessions: false\n')
        if config.exists() and config.read_text(encoding='utf-8') != expected:
            raise ValueError('Dedicated browser configuration changed; review before running')
        config.write_text(expected, encoding='utf-8', newline='')
        os.environ['HERMES_HOME'] = str(browser_home)
        for key in ('BROWSERBASE_API_KEY', 'BROWSER_USE_API_KEY', 'FIRECRAWL_API_KEY',
                    'FIRECRAWL_API_URL', 'AGENT_BROWSER_CDP', 'AGENT_BROWSER_PROFILE'):
            os.environ.pop(key, None)
        result = browser_run(data, receipt_path, limit=limit, venue_ids=venue_ids)
        status = build_status(data, read_json(receipt_path))
        execution.update(finished_at=now(), state='blocked' if result['stopped'] else 'complete',
                         source_commit=commit, release_id=read_json(data / 'directory-release.json')['release_id'],
                         observations_written=result['attempted'], coverage=status['counts'])
        status.update(provider='hermes-browser', run_id=execution['execution_id'], run_attempt=1,
                      run_url='', source_execution_finished_at=execution['finished_at'])
        atomic_json(state_dir / 'hermes-google-status.json', status)
        execution['status_sha256'] = hashlib.sha256((state_dir / 'hermes-google-status.json').read_bytes()).hexdigest()
        atomic_json(state_dir / 'last-run.json', execution)
        return execution
    except Exception as error:
        execution.update(finished_at=now(), state='failed', failure_code=type(error).__name__)
        atomic_json(state_dir / 'last-run.json', execution)
        raise RuntimeError('Hermes Google comparison failed; inspect the local execution receipt') from None
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--limit', type=int, default=45)
    parser.add_argument('--venue-id', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(execute(args.state_dir, args.limit, args.venue_id)))


if __name__ == '__main__':
    main()
