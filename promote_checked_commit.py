"""Advance main only after GitHub's existing validation job passes on these exact bytes."""
import json
import os
import re
import subprocess
import time

from request_pages_release import _json_request

# Automation may promote reviewed release data. Code changes use a PR.
DATA_PATHS = {
    'latest-bass-directory.pdf', 'pubs.csv', 'directory-meta.json', 'pub-coordinates.json',
    'venue-coordinates.json', 'coordinate-verification.json', 'venue-registry.json',
    'directory-release.json', 'location-review.json', 'venue-coordinate-overrides.json',
    'audit/manual-place-checks.csv',
}


def git(*args):
    return subprocess.check_output(['git', *args]).decode('utf-8').rstrip('\r\n')


def remote_head(ref, run):
    result = run('ls-remote', 'origin', ref)
    return result.split()[0] if result else ''


def promote(repository, token, execution, *, run=git, request=_json_request,
            sleep=time.sleep, monotonic=time.monotonic, timeout=1200):
    if not re.fullmatch(r'[0-9]+-[0-9]+', execution) or not token:
        raise ValueError('The workflow run, attempt and repository token are required.')
    sha = run('rev-parse', 'HEAD')
    parent = run('rev-parse', 'HEAD^')
    paths = set(run('diff', '--name-only', '-z', parent, sha).rstrip('\0').split('\0'))
    if not paths or not paths <= DATA_PATHS:
        raise ValueError('Automatic promotion can only publish recognised data files.')
    main = 'refs/heads/main'
    live = remote_head(main, run)
    if live == sha:
        return sha
    if live != parent:
        raise ValueError('main advanced. Rebuild the candidate against its current parent.')
    branch = 'codex/validated-' + execution
    ref = 'refs/heads/' + branch
    api = f'https://api.github.com/repos/{repository}/actions'
    existing = remote_head(ref, run)
    if existing and existing != sha:
        raise ValueError('The run-specific candidate ref is in use by a different commit.')
    if not existing:
        run('push', f'--force-with-lease={ref}:', 'origin', f'{sha}:{ref}')
    try:
        response = request(api + '/workflows/validate-coordinates.yml/dispatches', token=token,
                           method='POST', data=json.dumps({'ref': branch}).encode())
        run_id = response.get('workflow_run_id')
        if type(run_id) is not int or run_id < 1:
            raise ValueError('GitHub did not identify the validation execution.')
        print(f'Checking {sha} in https://github.com/{repository}/actions/runs/{run_id}', flush=True)
        deadline = monotonic() + timeout
        while True:
            check = request(f'{api}/runs/{run_id}', token=token)
            if (check.get('head_sha'), check.get('head_branch'), check.get('event')) != (sha, branch, 'workflow_dispatch'):
                raise ValueError('The validation execution is for a different candidate.')
            if check.get('status') == 'completed':
                if check.get('conclusion') != 'success':
                    raise ValueError(f"Candidate validation ended with {check.get('conclusion')}.")
                break
            if monotonic() >= deadline:
                raise TimeoutError('Timed out awaiting the candidate validation. main was not advanced.')
            sleep(10)
        if remote_head(main, run) != parent:
            raise ValueError('main advanced while the candidate was being validated.')
        try:
            run('push', 'origin', f'{sha}:{main}')
        except subprocess.CalledProcessError:
            if remote_head(main, run) != sha:
                raise
        if remote_head(main, run) != sha:
            raise RuntimeError('GitHub did not confirm publication of the validated commit.')
        return sha
    finally:
        # Only remove our own ref if it still points to our candidate.
        try:
            run('push', f'--force-with-lease={ref}:{sha}', 'origin', ':' + ref)
        except subprocess.CalledProcessError:
            print('::warning::The run-specific validation branch could not be removed.')


if __name__ == '__main__':
    promote(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'],
            os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT'])
