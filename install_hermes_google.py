"""Install a committed BassBeerMap worker for the existing Hermes cron job."""

import argparse
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


BASS_JOB_ID = '1353548e1bb7'


def verify_scheduler(jobs_file, launcher):
    jobs = json.loads(Path(jobs_file).read_text(encoding='utf-8'))['jobs']
    matches = [job for job in jobs if job.get('id') == BASS_JOB_ID]
    if len(matches) != 1:
        raise ValueError('Expected exactly one native BassBeerMap job')
    job = matches[0]
    if (job.get('enabled') is not True or job.get('no_agent') is not True
            or job.get('state') != 'scheduled' or job.get('schedule', {}).get('kind') != 'cron'
            or Path(job.get('script') or '').resolve() != Path(launcher).resolve()
            or not Path(job.get('interpreter') or '').is_file()
            or any(job.get(key) for key in ('arguments', 'args', 'script_args', 'monitor_script', 'monitor_url'))):
        raise ValueError('Native BassBeerMap job must run the production launcher as an enabled recurring Python script')


def production_launcher(app, jobs_file):
    return ('"""Launch the scheduled BassBeerMap Google review and publisher."""\n'
            'import sys\n'
            f'sys.path.insert(0, {str(app)!r})\n'
            'from hermes_google_runner import run_production\n'
            f'raise SystemExit(run_production({str(app.parent)!r}, {str(jobs_file)!r}, __file__))\n').encode('utf-8')


def verify_installation(state, launcher, jobs_file, commit):
    verify_scheduler(jobs_file, launcher)
    if len(commit) != 40 or any(character not in '0123456789abcdef' for character in commit):
        raise ValueError('Installation check requires the full Git commit SHA')
    app = state / ('app-' + commit)
    if launcher.read_bytes() != production_launcher(app, jobs_file):
        raise ValueError('Installed BassBeerMap launcher differs from the publishing launcher')
    manifest = json.loads((app / 'implementation.json').read_text(encoding='utf-8'))
    if (manifest['commit'] != commit or not manifest['files']
            or any(hashlib.sha256((app / name).read_bytes()).hexdigest() != digest
                   for name, digest in manifest['files'].items())):
        raise ValueError('Installed BassBeerMap code differs from its revision manifest')


def install(repo, revision, state, launcher, jobs_file):
    repo, state, launcher = (path.resolve() for path in (repo, state, launcher))
    jobs_file = jobs_file.resolve()
    verify_scheduler(jobs_file, launcher)

    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])

    commit = git('rev-parse', '--verify', revision + '^{commit}').decode().strip()
    sources = [name for name in git('ls-tree', '--name-only', commit).decode().splitlines()
               if name.endswith('.py')]
    payload = git('archive', '--format=tar', commit, '--', *sources)
    app = state / ('app-' + commit)
    hashes = {}
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bass-map-install-', dir=state) as temporary:
        staged = Path(temporary) / 'app'
        staged.mkdir()
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for name in sources:
                data = archive.extractfile(name).read()
                compile(data, name, 'exec')
                (staged / name).write_bytes(data)
                hashes[name] = hashlib.sha256(data).hexdigest()
        manifest = {'commit': commit, 'files': hashes}
        runner = (staged / 'hermes_google_runner.py').read_text(encoding='utf-8')
        if 'def run_production(' not in runner:
            raise ValueError('Revision lacks the guarded production entry point')
        (staged / 'implementation.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        if app.exists():
            actual = {name: hashlib.sha256((app / name).read_bytes()).hexdigest() for name in sources}
            if actual != hashes or json.loads((app / 'implementation.json').read_text()) != manifest:
                raise ValueError(f'Installed revision has modified files: {app}')
        else:
            os.replace(staged, app)

    hermes_home = jobs_file.parent.parent
    os.environ['HERMES_HOME'] = str(hermes_home)
    sys.path.insert(0, str(hermes_home / 'hermes-agent'))
    jobs = importlib.import_module('cron.jobs')
    executions = importlib.import_module('cron.executions')
    if jobs._current_cron_store().jobs_file.resolve() != jobs_file:
        raise ValueError('Installer resolved a different native scheduler store')
    with jobs._fire_job_lock(BASS_JOB_ID, blocking=False) as acquired:
        if (not acquired or executions.live_inflight_execution(BASS_JOB_ID)
                or any((state / name).exists() for name in ('runner.lock', 'observations.json.lock'))):
            raise ValueError('Native BassBeerMap job is running or its scheduler lock is unavailable')
        verify_scheduler(jobs_file, launcher)
        with tempfile.NamedTemporaryFile('wb', dir=launcher.parent, delete=False) as handle:
            handle.write(production_launcher(app, jobs_file))
            staged_launcher = Path(handle.name)
        try:
            os.replace(staged_launcher, launcher)
        finally:
            staged_launcher.unlink(missing_ok=True)
        verify_installation(state, launcher, jobs_file, commit)
    return commit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    parser.add_argument('--scheduler-jobs', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Verify the installed revision and native scheduler without changes')
    args = parser.parse_args()
    if args.check:
        verify_installation(args.state_dir.resolve(), args.launcher.resolve(), args.scheduler_jobs.resolve(), args.revision)
        print(args.revision)
    else:
        print(install(Path(__file__).resolve().parent, args.revision, args.state_dir, args.launcher, args.scheduler_jobs))
