"""Request and verify the branch-based GitHub Pages release after workflow pushes."""
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


API_VERSION = '2026-03-10'
ASSET_PATHS = ('index.html', 'directory-release.json', 'latest-bass-directory.pdf', 'pubs.csv')
WORKER_CONFIG_URL = 'https://bassbeermap.bristolbagpiper.workers.dev/api/report-problem?config=1'
VISITOR_CHECKS = {
    'index.html': (b'id="problemReportForm"', b'directory-release.json',
                   b'latest-bass-directory.pdf',
                   b'https://bassbeermap.bristolbagpiper.workers.dev/api/report-problem'),
}


class PagesReleaseError(RuntimeError):
    pass


def _json_request(url, *, token, method='GET', data=None, opener=urllib.request.urlopen):
    headers = {'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': API_VERSION,
               'User-Agent': 'BassBeerMap-Pages-release-check'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener(request, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise PagesReleaseError(f'GitHub API returned HTTP {error.code} for {method} {url.rsplit("/", 1)[-1]}') from None
    except (OSError, ValueError) as error:
        raise PagesReleaseError(f'GitHub API request failed: {type(error).__name__}') from None


def _asset_bytes(path, *, opener=urllib.request.urlopen):
    request = urllib.request.Request(path, headers={'Cache-Control': 'no-cache',
                                                    'User-Agent': 'BassBeerMap-Pages-readback'})
    try:
        with opener(request, timeout=30) as response:
            status = getattr(response, 'status', 200)
            if status != 200:
                raise PagesReleaseError(f'Visitor readback returned HTTP {status} for {path}')
            return response.read()
    except urllib.error.HTTPError as error:
        raise PagesReleaseError(f'Visitor readback returned HTTP {error.code} for {path}') from None
    except OSError as error:
        raise PagesReleaseError(f'Visitor readback failed for {path}: {type(error).__name__}') from None


def _git_bytes(commit, path, *, run=subprocess.run):
    try:
        result = run(['git', 'show', f'{commit}:{path}'], check=True, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        raise PagesReleaseError(f'Committed Pages asset could not be read: {path}') from error
    return result.stdout


def _check_site_mode(site):
    source = site.get('source') or {}
    if site.get('build_type') != 'legacy' or source.get('branch') != 'main' or source.get('path') != '/':
        raise PagesReleaseError('GitHub Pages configuration changed from branch-based main-root publishing; stop and reassess.')
    if (site.get('cname') or '').lower() != 'bassbeermap.com':
        raise PagesReleaseError('GitHub Pages custom domain no longer matches bassbeermap.com; stop and reassess.')


def _check_public_assets(expected, *, base_url, opener):
    for path, committed in expected.items():
        url = base_url.rstrip('/') + ('/' if path == 'index.html' else '/' + path)
        served = _asset_bytes(url, opener=opener)
        if hashlib.sha256(served).digest() != hashlib.sha256(committed).digest():
            return False, path
        required = VISITOR_CHECKS.get(path, ())
        if any(fragment not in served for fragment in required):
            raise PagesReleaseError(f'Visitor path does not contain required {path} behavior.')
    return True, None


def _feedback_configured(opener):
    try:
        config = json.loads(_asset_bytes(WORKER_CONFIG_URL, opener=opener))
        return isinstance(config, dict) and bool(config.get('turnstileSiteKey'))
    except (PagesReleaseError, UnicodeDecodeError, ValueError):
        return False


def publish_and_verify(*, repository, expected_commit, token, base_url='https://bassbeermap.com',
                       timeout_seconds=600, poll_seconds=10, opener=urllib.request.urlopen,
                       sleep=time.sleep, run=subprocess.run, monotonic=time.monotonic):
    if not repository or '/' not in repository or not expected_commit:
        raise PagesReleaseError('Repository and expected commit are required.')
    if not token:
        raise PagesReleaseError('GITHUB_TOKEN is missing; Pages:write is required for the build request.')

    current = run(['git', 'rev-parse', 'HEAD'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
    if current != expected_commit:
        raise PagesReleaseError(f'Checkout HEAD {current} does not match expected release {expected_commit}.')

    api = f'https://api.github.com/repos/{repository}'
    site = _json_request(api + '/pages', token=token, opener=opener)
    _check_site_mode(site)
    main = _json_request(api + '/commits/main', token=token, opener=opener)
    if main.get('sha') != expected_commit:
        raise PagesReleaseError('main advanced after publication; no Pages request was made.')

    expected = {path: _git_bytes(expected_commit, path, run=run) for path in ASSET_PATHS}
    latest_url = api + '/pages/builds/latest'
    latest = _json_request(latest_url, token=token, opener=opener)
    requested = False
    if latest.get('commit') != expected_commit or latest.get('status') == 'errored':
        _json_request(api + '/pages/builds', token=token, method='POST', data=b'{}', opener=opener)
        requested = True

    deadline = monotonic() + timeout_seconds
    build = latest
    while not (build.get('commit') == expected_commit and build.get('status') == 'built'):
        if (build.get('commit') == expected_commit and build.get('status') == 'errored'
                and (not requested or build != latest)):
            raise PagesReleaseError('GitHub Pages reported a failed build for the expected commit.')
        if monotonic() >= deadline:
            raise PagesReleaseError('Timed out waiting for GitHub Pages to build the expected commit.')
        sleep(poll_seconds)
        build = _json_request(latest_url, token=token, opener=opener)

    while True:
        try:
            matched, changed_path = _check_public_assets(expected, base_url=base_url, opener=opener)
            if matched:
                hashes = {path: hashlib.sha256(data).hexdigest() for path, data in expected.items()}
                return {'commit': expected_commit, 'build_status': 'built', 'build_requested': requested,
                        'asset_sha256': hashes, 'feedback_endpoint_configured': _feedback_configured(opener)}
            last_mismatch = changed_path
        except PagesReleaseError as error:
            last_mismatch = str(error)
        if monotonic() >= deadline:
            raise PagesReleaseError(f'Pages build completed but visitor readback did not match: {last_mismatch}')
        sleep(poll_seconds)


def main():
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    expected_commit = os.environ.get('EXPECTED_PAGES_COMMIT', '')
    token = os.environ.get('GH_TOKEN', '')
    result = publish_and_verify(repository=repository, expected_commit=expected_commit, token=token)
    print(json.dumps(result, sort_keys=True))
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as summary:
            summary.write('## GitHub Pages release readback\n\n')
            summary.write(f"- Expected commit: `{result['commit']}`\n")
            summary.write(f"- Pages build status: `{result['build_status']}`\n")
            summary.write(f"- Build requested in this run: `{str(result['build_requested']).lower()}`\n")
            for path, digest in result['asset_sha256'].items():
                summary.write(f'- Served `{path}` SHA-256: `{digest}`\n')
            feedback = 'configured' if result['feedback_endpoint_configured'] else 'unavailable or unconfigured'
            summary.write(f'- Feedback endpoint config GET: {feedback}\n')
    if not result['feedback_endpoint_configured']:
        print('::warning::The map is published. The public feedback endpoint configuration could not be verified.')


if __name__ == '__main__':
    main()
