"""Fetch the public directory, record local Google checks and send private alerts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

from directory_release import read_json
from hermes_google_alerts import notify
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
    for attempt in range(2):
        try:
            with urlopen(request, timeout=30) as response:
                data = response.read(max_bytes + 1)
            break
        except HTTPError as error:
            if attempt or error.code not in (500, 502, 503, 504) or error.headers.get('Retry-After'):
                raise
        except (TimeoutError, URLError) as error:
            reason = error.reason if isinstance(error, URLError) else error
            if attempt or not isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
                raise
        time.sleep(2)
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


def execute(state_dir=DEFAULT_STATE, limit=45, venue_ids=(), fetch=True, browser_run=browse,
            send_notification=notify):
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / 'runner.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    receipt_path = state_dir / 'observations.json'
    execution = dict(schema_version=1, provider='hermes-browser', execution_id=uuid.uuid4().hex,
                     started_at=now(), finished_at=None, state='running')
    stage = 'fetch published directory'
    status = None
    try:
        version = read_json(Path(__file__).with_name('implementation.json'), {})
        execution['implementation_commit'] = version.get('commit')
        atomic_json(state_dir / 'last-run.json', execution)
        data = state_dir / 'data'
        data.mkdir(exist_ok=True)
        commit = fetch_inputs(data) if fetch else None
        stage = 'configure browser'
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
        stage = 'compare venues in Google Maps'
        result = browser_run(data, receipt_path, limit=limit, venue_ids=venue_ids)
        stage = 'build review report'
        batch = {key: result.get(key, 0) for key in ('selected', 'attempted', 'checked', 'ambiguous', 'unavailable')}
        status = build_status(data, read_json(receipt_path), batch=batch)
        execution.update(finished_at=now(), state='blocked' if result['stopped'] else 'complete',
                         source_commit=commit, release_id=read_json(data / 'directory-release.json')['release_id'],
                         observations_written=result['attempted'], batch=batch,
                         coverage={key: value for key, value in status['counts'].items() if key != 'checked_this_run'})
        if result['stopped'] and result.get('detail'):
            execution['detail'] = result['detail']
        status.update(provider='hermes-browser', run_id=execution['execution_id'], run_attempt=1, batch=batch,
                      run_url='', source_execution_finished_at=execution['finished_at'])
        stage = 'save review report'
        atomic_json(state_dir / 'hermes-google-status.json', status)
        execution['status_sha256'] = hashlib.sha256((state_dir / 'hermes-google-status.json').read_bytes()).hexdigest()
    except Exception as error:
        execution.update(finished_at=now(), state='failed', failure_code=type(error).__name__,
                         detail=f'Could not {stage} ({type(error).__name__})')
        status = None
    finally:
        try:
            atomic_json(state_dir / 'last-run.json', execution)
            try:
                delivery = send_notification(state_dir, status, execution, atomic_json)
            except Exception as error:
                delivery = {'state': 'failed', 'detail': f'Mail processing failed ({type(error).__name__})'}
            execution['notification'] = delivery
            if delivery['state'] in ('failed', 'unconfirmed', 'not_configured'):
                execution['success'] = False
                reason = delivery.get('detail', f"Private email {delivery['state'].replace('_', ' ')}")
                execution['detail'] = '. '.join(filter(None, (execution.get('detail'), reason)))
            atomic_json(state_dir / 'last-run.json', execution)
        finally:
            lock.unlink(missing_ok=True)
    return execution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--limit', type=int, default=45)
    parser.add_argument('--venue-id', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(execute(args.state_dir, args.limit, args.venue_id)))


if __name__ == '__main__':
    main()
