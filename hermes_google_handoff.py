"""Publish a validated Hermes Google review snapshot with a leased Git push."""

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from hermes_google_publish import _stage_commit_data, git_executable, prepare_candidate
from hermes_google_review import parse_utc


REPOSITORY = 'bristolbagpiper/BassBeerMap'
PUBLIC_ORIGIN = f'https://github.com/{REPOSITORY}.git'
PUSH_URL = f'git@github.com:{REPOSITORY}.git'
STATUS_PATH = 'hermes-google-status.json'
MAX_PUBLISH_SECONDS = 32
GIT_TIMEOUT_SECONDS = 8
SAME_STATUS_REFRESH_AGE = timedelta(hours=23)
SHA_PATTERN = re.compile(r'^[0-9a-f]{40}$')
UNSAFE_SHELL = re.compile(r'[;&|<>`$()\r\n]')
AUTH_FAILURE_HINTS = (
    'permission denied', 'authentication failed', 'could not read username',
    'publickey', 'repository not found', 'access denied', '403', 'not allowed',
)
TRANSIENT_PUSH_HINTS = (
    'timed out', 'connection reset', 'could not resolve host', 'failed to connect',
    'network is unreachable', 'connection refused', 'remote end hung up',
)


class HandoffError(Exception):
    def __init__(self, phase, code):
        super().__init__(code)
        self.phase = phase
        self.code = code


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _safe_sha(value):
    return isinstance(value, str) and bool(SHA_PATTERN.fullmatch(value))


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path, *, required=True):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        if required:
            raise HandoffError('state', 'required_state_missing') from None
        return None
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise HandoffError('state', 'invalid_state_json') from None


def _base_env(git_executable, *, ssh_command=None):
    env = os.environ.copy()
    for key in tuple(env):
        if (key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_', 'GIT_TRACE'))
                or key in {
                    'GIT_ASKPASS', 'SSH_ASKPASS', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS',
                    'GIT_CONFIG', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM',
                    'GIT_SSH', 'GIT_SSH_VARIANT', 'GIT_CURL_VERBOSE', 'GIT_HTTP_EXTRAHEADER',
                    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                    'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GITHUB_TOKEN', 'GH_TOKEN',
                }):
            env.pop(key, None)
    env.update({
        'GIT_TERMINAL_PROMPT': '0',
        'GCM_INTERACTIVE': 'never',
        'GCM_GUI_PROMPT': '0',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': 'NUL' if os.name == 'nt' else os.devnull,
    })
    if ssh_command:
        env['GIT_SSH_COMMAND'] = ssh_command
    else:
        env.pop('GIT_SSH_COMMAND', None)
    git_dir = str(Path(git_executable).resolve().parent)
    prior_path = env.get('PATH', '')
    if git_dir.lower() not in {part.lower() for part in prior_path.split(os.pathsep)}:
        env['PATH'] = git_dir + (os.pathsep + prior_path if prior_path else '')
    return env


def _run_git(git, args, cwd, env, *, phase, timeout=GIT_TIMEOUT_SECONDS, input_bytes=None):
    try:
        result = subprocess.run(
            [git, *args], cwd=cwd, env=env, input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(0.25, timeout), check=False,
        )
    except FileNotFoundError:
        raise HandoffError(phase, 'git_unavailable') from None
    except subprocess.TimeoutExpired:
        raise HandoffError(phase, 'git_timeout') from None
    except OSError:
        raise HandoffError(phase, 'git_process_error') from None
    output = result.stdout.decode('utf-8', errors='replace').strip()
    error = result.stderr.decode('utf-8', errors='replace').strip()
    return result.returncode, output, error


def _git_checked(git, args, cwd, env, *, phase, timeout=GIT_TIMEOUT_SECONDS, input_bytes=None):
    code, output, _ = _run_git(git, args, cwd, env, phase=phase,
                               timeout=timeout, input_bytes=input_bytes)
    if code != 0:
        raise HandoffError(phase, f'{phase}_failed')
    return output


def _load_auth(state_dir):
    config = _read_json(Path(state_dir) / 'publication-auth.json')
    if not isinstance(config, dict) or set(config) != {'push_url', 'ssh_command'}:
        raise HandoffError('auth', 'invalid_auth_config')
    if config.get('push_url') != PUSH_URL:
        raise HandoffError('auth', 'invalid_push_target')
    command = config.get('ssh_command')
    if not isinstance(command, str) or len(command) > 2000 or UNSAFE_SHELL.search(command):
        raise HandoffError('auth', 'invalid_ssh_command')
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        raise HandoffError('auth', 'invalid_ssh_command') from None
    if (len(words) != 3 or words[1] != '-F'
            or not Path(words[0]).is_absolute() or not Path(words[0]).is_file()
            or not Path(words[2]).is_absolute() or not Path(words[2]).is_file()):
        raise HandoffError('auth', 'invalid_ssh_command')
    return {'push_url': PUSH_URL, 'ssh_command': command}


@contextlib.contextmanager
def _publish_lock(state_dir, timeout=18):
    path = Path(state_dir) / 'publication.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    start = time.monotonic()
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b'0')
            handle.flush()
        while not locked and time.monotonic() - start < timeout:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                time.sleep(0.2)
        if not locked:
            raise HandoffError('lock', 'handoff_busy')
        yield
    finally:
        if locked:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _attempt(state_dir, execution_id, phase, *, state='running', failure_code=None, now=None,
             published_commit=None):
    value = {
        'schema_version': 1,
        'state': state,
        'execution_id': execution_id,
        'phase': phase,
        'failure_code': failure_code,
        'updated_at': _iso(now or _utc_now()),
    }
    if published_commit is not None:
        value['published_commit'] = published_commit
    _write_json_atomic(Path(state_dir) / 'publication-attempt.json', value)
    return value


def _receipt(state_dir):
    value = _read_json(Path(state_dir) / 'publication-receipt.json', required=False)
    return value if isinstance(value, dict) else {}


def _write_receipt(state_dir, value):
    _write_json_atomic(Path(state_dir) / 'publication-receipt.json', value)


def _ensure_cache(cache, git, env):
    cache = Path(cache).resolve()
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not (cache / 'HEAD').is_file():
        code, _, _ = _run_git(git, ['init', '--bare', str(cache)], cache.parent, env,
                              phase='cache_init')
        if code != 0:
            raise HandoffError('cache_init', 'cache_init_failed')
    bare = _git_checked(git, ['rev-parse', '--is-bare-repository'], cache, env, phase='cache_check')
    if bare != 'true':
        raise HandoffError('cache_check', 'cache_not_bare')
    code, origin, _ = _run_git(git, ['remote', 'get-url', 'origin'], cache, env, phase='cache_origin')
    if code == 0:
        if origin != PUBLIC_ORIGIN:
            raise HandoffError('cache_origin', 'cache_origin_mismatch')
    else:
        _git_checked(git, ['remote', 'add', 'origin', PUBLIC_ORIGIN], cache, env, phase='cache_origin')
    return cache


def _fetch_main(git, cache, env, deadline):
    timeout = min(GIT_TIMEOUT_SECONDS, max(0.25, deadline - time.monotonic()))
    _git_checked(git, ['-c', 'credential.helper=', 'fetch', '--no-tags', 'origin',
                       '+refs/heads/main:refs/remotes/origin/main'],
                 cache, env, phase='fetch', timeout=timeout)
    tip = _git_checked(git, ['rev-parse', 'refs/remotes/origin/main'], cache, env, phase='fetch')
    if not _safe_sha(tip):
        raise HandoffError('fetch', 'invalid_remote_tip')
    return tip


def _read_blob(git, cache, commit, path, env, phase='remote_status'):
    code, _, _ = _run_git(git, ['cat-file', '-e', f'{commit}:{path}'], cache, env, phase=phase)
    if code != 0:
        return None
    code, output, _ = _run_git(git, ['show', f'{commit}:{path}'], cache, env, phase=phase)
    if code != 0:
        raise HandoffError(phase, 'remote_status_read_failed')
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise HandoffError(phase, 'remote_status_invalid') from None


def _remote_run_time(status):
    if not isinstance(status, dict) or status.get('provider') != 'hermes-browser':
        return None
    try:
        return parse_utc(status.get('source_execution_finished_at'))
    except ValueError:
        return None


def _same_semantics(existing, candidate):
    if not isinstance(existing, dict):
        return False
    fields = ('directory_binding_signature', 'issues', 'findings', 'configured', 'hard_failure', 'execution_state')
    if any(existing.get(field) != candidate.get(field) for field in fields):
        return False
    old_counts, new_counts = existing.get('counts'), candidate.get('counts')
    coverage = ('total', 'current', 'unchecked_or_stale')
    if not isinstance(old_counts, dict) or not isinstance(new_counts, dict):
        return False
    if any(old_counts.get(field) != new_counts.get(field) for field in coverage):
        return False
    return True


def _status_fingerprint_matches(existing, candidate):
    return bool(isinstance(existing, dict)
                and existing.get('run_id') == candidate.get('run_id')
                and existing.get('source_status_sha256') == candidate.get('source_status_sha256')
                and existing.get('directory_binding_signature') == candidate.get('directory_binding_signature'))


def _read_existing_report(git, cache, parent, env):
    status = _read_blob(git, cache, parent, STATUS_PATH, env)
    if status is not None and (not isinstance(status, dict) or status.get('provider') != 'hermes-browser'):
        raise HandoffError('remote_status', 'remote_status_unrecognized')
    return status


def _prepare_status(state_dir, cache, parent, now, expected_execution_id, env):
    with tempfile.TemporaryDirectory(prefix='hermes-google-publish-current-') as staged:
        try:
            _stage_commit_data(cache, parent, staged)
            candidate = prepare_candidate(state_dir, staged, parent, now=now, repository=cache)
        except HandoffError:
            raise
        except Exception:
            raise HandoffError('candidate', 'candidate_invalid') from None
    if expected_execution_id and candidate.get('run_id') != expected_execution_id:
        raise HandoffError('candidate', 'execution_mismatch')
    if candidate.get('execution_state') not in ('complete', 'blocked', 'failed'):
        raise HandoffError('candidate', 'execution_state_invalid')
    return candidate


def _latest_action_run(sha, *, timeout=3):
    query = urlencode({'head_sha': sha, 'per_page': 100})
    url = f'https://api.github.com/repos/{REPOSITORY}/actions/runs?{query}'
    request = Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'BassBeerMap-Hermes-Google-Handoff',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(1_000_000))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    runs = payload.get('workflow_runs', []) if isinstance(payload, dict) else []
    matches = [run for run in runs if isinstance(run, dict)
               and run.get('event') == 'push'
               and run.get('name') == 'Location review notifications'
               and run.get('head_sha') == sha]
    if not matches:
        return None
    run = sorted(matches, key=lambda item: item.get('id', 0), reverse=True)[0]
    return {
        'id': run.get('id'),
        'url': run.get('html_url'),
        'status': run.get('status'),
        'conclusion': run.get('conclusion'),
    }


def _confirm_actions(sha, *, lookup, deadline, wait_seconds):
    expires = min(deadline, time.monotonic() + max(0, wait_seconds))
    while True:
        run = lookup(sha)
        if run:
            return {'state': 'enqueued', **run}
        if time.monotonic() >= expires:
            return {'state': 'review_pending'}
        time.sleep(min(1.5, max(0, expires - time.monotonic())))


def _auth_failure(stderr):
    lower = stderr.lower()
    return any(hint in lower for hint in AUTH_FAILURE_HINTS)


def _transient_push_failure(stderr):
    lower = stderr.lower()
    return any(hint in lower for hint in TRANSIENT_PUSH_HINTS)


def _push_child(git, cache, env, push_url, parent, child, deadline):
    timeout = min(GIT_TIMEOUT_SECONDS, max(0.25, deadline - time.monotonic()))
    try:
        code, _, stderr = _run_git(
            git,
            ['push', '--porcelain', f'--force-with-lease=refs/heads/main:{parent}',
             push_url, f'{child}:refs/heads/main'],
            cache, env, phase='push', timeout=timeout,
        )
    except HandoffError as error:
        if error.code == 'git_timeout':
            return 'transient'
        raise
    if code == 0:
        return 'accepted'
    if _auth_failure(stderr):
        return 'auth_failed'
    if _transient_push_failure(stderr):
        return 'transient'
    return 'rejected'


def _write_status_child(git, cache, parent, candidate, env, deadline):
    payload = (json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    blob = _git_checked(git, ['hash-object', '-w', '--stdin'], cache, env,
                        phase='build_commit', timeout=min(GIT_TIMEOUT_SECONDS, max(.25, deadline-time.monotonic())),
                        input_bytes=payload)
    if not _safe_sha(blob):
        raise HandoffError('build_commit', 'invalid_blob_id')
    with tempfile.TemporaryDirectory(prefix='hermes-google-index-') as temporary:
        index = str(Path(temporary) / 'index')
        index_env = dict(env, GIT_INDEX_FILE=index)
        _git_checked(git, ['read-tree', parent], cache, index_env, phase='build_commit')
        _git_checked(git, ['update-index', '--add', '--cacheinfo', f'100644,{blob},{STATUS_PATH}'],
                     cache, index_env, phase='build_commit')
        tree = _git_checked(git, ['write-tree'], cache, index_env, phase='build_commit')
    commit_env = dict(env, GIT_AUTHOR_NAME='BassBeerMap Hermes',
                      GIT_AUTHOR_EMAIL='hermes-google@users.noreply.github.com',
                      GIT_COMMITTER_NAME='BassBeerMap Hermes',
                      GIT_COMMITTER_EMAIL='hermes-google@users.noreply.github.com')
    child = _git_checked(git, ['commit-tree', tree, '-p', parent,
                               '-m', 'Publish validated Hermes Google review status'],
                         cache, commit_env, phase='build_commit')
    if not _safe_sha(child):
        raise HandoffError('build_commit', 'invalid_commit_id')
    return child, blob, hashlib.sha256(payload).hexdigest()


def _receipt_value(candidate, *, state, published_commit, parent_commit,
                   blob_sha, status_sha256, public_status_sha256, actions,
                   verified_at, previous=None):
    return {
        'schema_version': 1,
        'state': state,
        'execution_id': candidate['run_id'],
        'finished_at': candidate['source_execution_finished_at'],
        'published_commit': published_commit,
        'parent_commit': parent_commit,
        'blob_sha': blob_sha,
        'status_sha256': status_sha256,
        'public_status_sha256': public_status_sha256,
        'directory_binding_signature': candidate['directory_binding_signature'],
        'verified_at': _iso(verified_at),
        'actions': actions,
        'transport': 'native-ssh',
        'previous_execution_id': previous.get('execution_id') if previous else None,
    }


def _existing_is_newer(existing, candidate):
    existing_time = _remote_run_time(existing)
    try:
        candidate_time = parse_utc(candidate.get('source_execution_finished_at'))
    except ValueError:
        return True
    return existing_time is not None and (
        existing_time > candidate_time
        or (existing_time == candidate_time and existing.get('run_id') != candidate['run_id'])
    )


def _fresh_existing(existing, now):
    value = _remote_run_time(existing)
    return bool(value and timedelta(0) <= now - value < SAME_STATUS_REFRESH_AGE)


def _report_commit(git, cache, parent, env):
    code, output, _ = _run_git(
        git, ['log', '-1', '--format=%H', parent, '--', STATUS_PATH], cache, env,
        phase='readback')
    if code != 0 or not _safe_sha(output):
        raise HandoffError('readback', 'report_commit_unavailable')
    return output


def _run_locked(state_dir, expected_execution_id, cache, *, now, force_publish=False,
                actions_lookup, actions_wait_seconds, deadline):
    receipt = _receipt(state_dir)
    execution = _read_json(Path(state_dir) / 'last-run.json')
    execution_id = execution.get('execution_id') if isinstance(execution, dict) else None
    _attempt(state_dir, execution_id, 'validate_local_run', now=now)
    if expected_execution_id and execution_id != expected_execution_id:
        raise HandoffError('validate_local_run', 'execution_mismatch')
    if (Path(state_dir) / 'runner.lock').exists() or (Path(state_dir) / 'observations.json.lock').exists():
        raise HandoffError('validate_local_run', 'active_execution')

    auth = _load_auth(state_dir)
    git = git_executable()
    env = _base_env(git)
    push_env = _base_env(git, ssh_command=auth['ssh_command'])
    cache = _ensure_cache(cache, git, env)
    try:
        parent = _fetch_main(git, cache, env, deadline)
    except HandoffError as error:
        if error.code not in ('fetch_failed', 'git_timeout'):
            raise
        time.sleep(1)
        parent = _fetch_main(git, cache, env, deadline)

    for attempt_index in range(2):
        _attempt(state_dir, execution_id, 'prepare_candidate', now=_utc_now())
        try:
            candidate = _prepare_status(state_dir, cache, parent, _utc_now(),
                                        expected_execution_id, env)
        except HandoffError:
            raise
        except Exception:
            raise HandoffError('prepare_candidate', 'candidate_invalid') from None
        existing = _read_existing_report(git, cache, parent, env)
        if existing is not None:
            if _existing_is_newer(existing, candidate):
                raise HandoffError('compare_remote', 'newer_remote_report')
            if (existing.get('run_id') == candidate['run_id']
                    and existing.get('source_status_sha256') != candidate['source_status_sha256']):
                raise HandoffError('compare_remote', 'same_run_source_hash_mismatch')
            same_run = _status_fingerprint_matches(existing, candidate)
            same_meaning = _same_semantics(existing, candidate)
            if same_run:
                if not same_meaning:
                    raise HandoffError('compare_remote', 'same_run_content_mismatch')
                return {'state': 'unchanged', 'execution_id': candidate['run_id'],
                        'published_commit': _report_commit(git, cache, parent, env)}
            if (not force_publish and same_meaning and _fresh_existing(existing, _utc_now())
                    and receipt.get('transport') == 'native-ssh'):
                return {'state': 'unchanged', 'execution_id': candidate['run_id'],
                        'published_commit': _report_commit(git, cache, parent, env)}

        child, blob_sha, status_sha = _write_status_child(git, cache, parent, candidate, push_env, deadline)
        _attempt(state_dir, execution_id, 'push_with_lease', now=_utc_now())
        push_result = _push_child(git, cache, push_env, auth['push_url'], parent, child, deadline)

        # A push can succeed remotely while the client sees a timeout. Always
        # read public main back before deciding whether to retry or fail.
        _attempt(state_dir, execution_id, 'verify_public_readback', now=_utc_now())
        try:
            readback = _fetch_main(git, cache, env, deadline)
        except HandoffError:
            if push_result == 'accepted':
                raise HandoffError('verify_public_readback', 'push_readback_unavailable') from None
            raise
        readback_status = _read_existing_report(git, cache, readback, env)
        if (readback_status is not None
                and _status_fingerprint_matches(readback_status, candidate)
                and _same_semantics(readback_status, candidate)):
            actual_blob = _git_checked(git, ['rev-parse', f'{readback}:{STATUS_PATH}'], cache, env,
                                       phase='verify_public_readback')
            actual_commit = _report_commit(git, cache, readback, env)
            if actual_blob != blob_sha or actual_commit != child:
                raise HandoffError('verify_public_readback', 'published_object_mismatch')
            actions = _confirm_actions(actual_commit, lookup=actions_lookup, deadline=deadline,
                                       wait_seconds=actions_wait_seconds)
            result_state = 'published' if actions.get('state') == 'enqueued' else 'review_pending'
            updated = _receipt_value(candidate, state=result_state, published_commit=actual_commit,
                                     parent_commit=parent, blob_sha=actual_blob,
                                     status_sha256=candidate['source_status_sha256'],
                                     public_status_sha256=status_sha, actions=actions,
                                     verified_at=_utc_now(), previous=receipt)
            _write_receipt(state_dir, updated)
            return {'state': result_state, 'execution_id': candidate['run_id'],
                    'published_commit': actual_commit, 'parent_commit': parent,
                    'blob_sha': actual_blob, 'actions': actions}

        if readback_status is not None and _existing_is_newer(readback_status, candidate):
            raise HandoffError('verify_public_readback', 'newer_remote_report')
        if readback != parent:
            if attempt_index == 0:
                # Rebuild once against the new exact remote parent, preserving
                # lease safety and rechecking all source identity bindings.
                parent = readback
                continue
            raise HandoffError('push_with_lease', 'remote_advanced_twice')
        if push_result == 'transient' and attempt_index == 0:
            continue
        code = {'auth_failed': 'push_auth_failed', 'rejected': 'push_rejected'}.get(push_result, 'push_failed')
        raise HandoffError('push_with_lease', code)

    raise HandoffError('push_with_lease', 'push_failed')


def publish_latest(state_dir, expected_execution_id=None, *, repository=None, force_publish=False,
                   now=None, actions_lookup=None, actions_wait_seconds=20):
    """Publish the latest complete local run; return a compact safe outcome."""
    state_dir = Path(state_dir).resolve()
    cache = Path(repository).resolve() if repository else state_dir / 'publication-repo.git'
    started = now or _utc_now()
    if started.tzinfo is None or started.utcoffset() != timedelta(0):
        return {'state': 'blocked', 'phase': 'validate_clock', 'failure_code': 'clock_not_utc'}
    deadline = time.monotonic() + MAX_PUBLISH_SECONDS
    execution_id = expected_execution_id
    receipt = {}
    try:
        with _publish_lock(state_dir):
            receipt = _receipt(state_dir)
            local_execution = _read_json(Path(state_dir) / 'last-run.json')
            if isinstance(local_execution, dict):
                execution_id = local_execution.get('execution_id')
            _attempt(state_dir, execution_id, 'start', now=started)
            if not _safe_sha(execution_id) and not re.fullmatch(r'[0-9a-f]{32}', str(execution_id or '')):
                raise HandoffError('validate_local_run', 'invalid_execution_id')
            if expected_execution_id and execution_id != expected_execution_id:
                raise HandoffError('validate_local_run', 'execution_mismatch')
            lookup = actions_lookup or _latest_action_run
            result = _run_locked(state_dir, expected_execution_id, cache, now=started,
                                 force_publish=force_publish,
                                 actions_lookup=lookup,
                                 actions_wait_seconds=actions_wait_seconds, deadline=deadline)
            _attempt(state_dir, execution_id, result['state'], state=result['state'], now=_utc_now(),
                     published_commit=result['published_commit'])
            return result
    except HandoffError as error:
        failure = error.code
        phase = error.phase
    except Exception:
        failure = 'unexpected_handoff_error'
        phase = 'publish'
    finished = _utc_now()
    try:
        _attempt(state_dir, execution_id, phase, state='blocked',
                 failure_code=failure, now=finished)
    except Exception:
        pass
    return {'state': 'blocked', 'execution_id': execution_id,
            'phase': phase, 'failure_code': failure}
