import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import hermes_google_handoff as handoff
from hermes_google_publish import git_executable


def write_exact_json(path, value):
    Path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def git(args, cwd):
    return subprocess.run([git_executable(), *args], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


class HermesGoogleHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from directory_release import read_json
        from build_place_review_queue import build_queue
        cls.bundle = read_json(Path(__file__).resolve().parents[1] / 'directory-release.json')
        cls.registry = read_json(Path(__file__).resolve().parents[1] / 'venue-registry.json')
        cls.item = build_queue(cls.bundle, cls.registry, {})[0]

    def make_case(self, run_state='complete'):
        from tests.test_hermes_google_publish import HermesGooglePublishTests
        runtime = tempfile.TemporaryDirectory()
        base = Path(runtime.name)
        helper = HermesGooglePublishTests()
        helper.bundle, helper.registry, helper.item = self.bundle, self.registry, self.item
        helper_temp, state_dir, source_data, execution = helper.make_case(run_state)
        self.addCleanup(helper_temp.cleanup)
        repo = base / 'repo'
        repo.mkdir()
        git(['init', '-b', 'main'], repo)
        git(['config', 'user.name', 'Offline test'], repo)
        git(['config', 'user.email', 'offline@example.invalid'], repo)
        for name in ('directory-release.json', 'venue-registry.json'):
            (repo / name).write_bytes((state_dir / 'data' / name).read_bytes())
        git(['add', 'directory-release.json', 'venue-registry.json'], repo)
        git(['commit', '-m', 'seed test directory'], repo)
        source_commit = git(['rev-parse', 'HEAD'], repo)
        execution = json.loads((state_dir / 'last-run.json').read_text(encoding='utf-8'))
        if execution.get('state') != 'failed':
            execution['source_commit'] = source_commit
            write_exact_json(state_dir / 'last-run.json', execution)
        remote = base / 'remote.git'
        git(['init', '--bare', str(remote)], base)
        git(['remote', 'add', 'origin', str(remote)], repo)
        git(['push', '-u', 'origin', 'main'], repo)
        self.addCleanup(runtime.cleanup)
        return state_dir, remote

    def call_publish(self, state_dir, remote, *, lookup=None, force_publish=False):
        with patch.object(handoff, 'PUBLIC_ORIGIN', str(remote)), \
             patch.object(handoff, 'PUSH_URL', str(remote)), \
             patch.object(handoff, '_load_auth', return_value={
                 'push_url': str(remote), 'ssh_command': None,
             }):
            return handoff.publish_latest(
                state_dir, force_publish=force_publish, actions_lookup=lookup or (lambda sha: {
                    'id': 7, 'url': 'https://github.com/example/run/7',
                    'status': 'queued', 'conclusion': None,
                }), actions_wait_seconds=0)

    def make_fresh_run(self, state_dir, execution_id, finished):
        from hermes_google_review import build_status

        execution_path = state_dir / 'last-run.json'
        execution = json.loads(execution_path.read_text(encoding='utf-8'))
        started = finished - timedelta(minutes=3)
        checked = finished - timedelta(minutes=2)
        attempted = finished - timedelta(minutes=2, seconds=30)
        observations_path = state_dir / 'observations.json'
        observations = json.loads(observations_path.read_text(encoding='utf-8'))
        observations['observations'][0]['attempted_at'] = attempted.isoformat().replace('+00:00', 'Z')
        observations['observations'][0]['checked_at'] = checked.isoformat().replace('+00:00', 'Z')
        write_exact_json(observations_path, observations)
        batch = execution['batch']
        status = build_status(state_dir / 'data', observations, now=checked, batch=batch)
        status.update(provider='hermes-browser', run_id=execution_id, run_attempt=1, run_url='',
                      batch=batch, source_execution_finished_at=finished.isoformat().replace('+00:00', 'Z'))
        status_path = state_dir / 'hermes-google-status.json'
        write_exact_json(status_path, status)
        execution.update(execution_id=execution_id,
                         started_at=started.isoformat().replace('+00:00', 'Z'),
                         finished_at=finished.isoformat().replace('+00:00', 'Z'),
                         observations_sha256=hashlib.sha256(observations_path.read_bytes()).hexdigest(),
                         observations_written=batch['attempted'],
                         status_sha256=hashlib.sha256(status_path.read_bytes()).hexdigest())
        write_exact_json(execution_path, execution)

    def test_publishes_only_status_and_receipt_tracks_actual_commit(self):
        state_dir, remote = self.make_case()
        result = self.call_publish(state_dir, remote)
        self.assertEqual(result['state'], 'published')
        self.assertEqual(git(['show', 'main:hermes-google-status.json'], remote)[:1], '{')
        paths = set(git(['ls-tree', '-r', '--name-only', 'main'], remote).splitlines())
        self.assertEqual(paths, {'directory-release.json', 'venue-registry.json', 'hermes-google-status.json'})
        receipt = json.loads((state_dir / 'publication-receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['published_commit'], result['published_commit'])
        self.assertEqual(receipt['transport'], 'native-ssh')
        self.assertEqual(len(receipt['status_sha256']), 64)
        self.assertEqual(len(receipt['public_status_sha256']), 64)

    def test_exact_replay_is_unchanged_and_preserves_success_receipt(self):
        state_dir, remote = self.make_case()
        first = self.call_publish(state_dir, remote)
        receipt_path = state_dir / 'publication-receipt.json'
        before = receipt_path.read_bytes()
        second = self.call_publish(state_dir, remote)
        self.assertEqual(second['state'], 'unchanged')
        self.assertEqual(receipt_path.read_bytes(), before)
        self.assertEqual(first['published_commit'], second['published_commit'])

    def test_scheduler_force_refresh_publishes_distinct_run_with_same_content(self):
        first_state, remote = self.make_case()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.make_fresh_run(first_state, 'c' * 32, now - timedelta(minutes=1))
        first = self.call_publish(first_state, remote)
        self.assertEqual(first['state'], 'published')
        first_status = json.loads(git(['show', 'main:hermes-google-status.json'], remote))

        second_state, _ = self.make_case()
        second_id = 'd' * 32
        self.make_fresh_run(second_state, second_id, now)
        second_execution = json.loads((second_state / 'last-run.json').read_text(encoding='utf-8'))
        self.assertNotEqual(first['execution_id'], second_execution['execution_id'])
        write_exact_json(second_state / 'publication-receipt.json',
                         json.loads((first_state / 'publication-receipt.json').read_text(encoding='utf-8')))

        ordinary = self.call_publish(second_state, remote)
        self.assertEqual(ordinary['state'], 'unchanged', ordinary)
        self.assertEqual(ordinary['execution_id'], second_execution['execution_id'])
        still_old = json.loads(git(['show', 'main:hermes-google-status.json'], remote))
        self.assertEqual(still_old['run_id'], first_status['run_id'])

        refreshed = self.call_publish(second_state, remote, force_publish=True)
        self.assertEqual(refreshed['state'], 'published')
        self.assertEqual(refreshed['execution_id'], second_execution['execution_id'])
        current = json.loads(git(['show', 'main:hermes-google-status.json'], remote))
        self.assertEqual(current['run_id'], second_execution['execution_id'])
        self.assertNotEqual(refreshed['published_commit'], first['published_commit'])

    def test_blocked_run_publishes_diagnostic_only_snapshot(self):
        state_dir, remote = self.make_case('blocked')
        result = self.call_publish(state_dir, remote)
        self.assertEqual(result['state'], 'published')
        status = json.loads(git(['show', 'main:hermes-google-status.json'], remote))
        self.assertEqual(status['execution_state'], 'blocked')
        self.assertEqual(status['findings'], [])
        self.assertEqual(status['counts']['current'], 0)

    def test_failed_run_publishes_diagnostic_only_snapshot(self):
        state_dir, remote = self.make_case('failed')
        result = self.call_publish(state_dir, remote)
        self.assertEqual(result['state'], 'published')
        status = json.loads(git(['show', 'main:hermes-google-status.json'], remote))
        self.assertEqual(status['execution_state'], 'failed')
        self.assertEqual(status['findings'], [])
        self.assertEqual(status['counts']['current'], 0)

    def test_lease_push_timeout_is_read_back_before_failure(self):
        state_dir, remote = self.make_case()
        original_push = handoff._push_child

        def push_then_timeout(git_path, cache, env, push_url, parent, child, deadline):
            original_push(git_path, cache, env, push_url, parent, child, deadline)
            return 'transient'

        with patch.object(handoff, '_push_child', side_effect=push_then_timeout):
            result = self.call_publish(state_dir, remote)
        self.assertEqual(result['state'], 'published')

    def test_authentication_failure_keeps_completed_observations_and_remote_main(self):
        state_dir, remote = self.make_case()
        observations = (state_dir / 'observations.json').read_bytes()
        head = git(['rev-parse', 'main'], remote)
        original = handoff._run_git

        def reject(git_path, args, *rest, **kwargs):
            if args[0] == 'push':
                return 128, '', 'Permission denied (publickey): private diagnostic'
            return original(git_path, args, *rest, **kwargs)

        with patch.object(handoff, '_run_git', side_effect=reject):
            result = self.call_publish(state_dir, remote)
        self.assertEqual(result['failure_code'], 'push_auth_failed')
        self.assertEqual(git(['rev-parse', 'main'], remote), head)
        self.assertEqual((state_dir / 'observations.json').read_bytes(), observations)
        self.assertFalse((state_dir / 'publication-receipt.json').exists())
        self.assertNotIn('private diagnostic', (state_dir / 'publication-attempt.json').read_text())

    def test_remote_advance_rebuilds_on_the_new_parent(self):
        state_dir, remote = self.make_case()
        checkout = remote.parent / 'repo'
        original = handoff._push_child
        concurrent = []

        def advance(git_path, cache, env, push_url, parent, child, deadline):
            if not concurrent:
                (checkout / 'directory-change.txt').write_text('parallel update', encoding='utf-8')
                git(['add', 'directory-change.txt'], checkout)
                git(['commit', '-m', 'Advance main independently'], checkout)
                git(['push', 'origin', 'main'], checkout)
                concurrent.append(git(['rev-parse', 'HEAD'], checkout))
            return original(git_path, cache, env, push_url, parent, child, deadline)

        with patch.object(handoff, '_push_child', side_effect=advance):
            result = self.call_publish(state_dir, remote)
        self.assertEqual(result['state'], 'published')
        self.assertEqual(git(['rev-parse', 'main^'], remote), concurrent[0])
        self.assertEqual(git(['show', 'main:directory-change.txt'], remote), 'parallel update')

    def test_inherited_git_auth_and_trace_overrides_are_removed(self):
        values = {
            'GIT_TRACE2_EVENT': 'secret-path', 'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_PARAMETERS': 'credential.helper=evil', 'GIT_SSH': 'evil',
            'GIT_SSH_VARIANT': 'plink', 'GIT_ASKPASS': 'evil',
            'GIT_TRACE_CURL': '1', 'GIT_HTTP_EXTRAHEADER': 'Authorization: secret',
        }
        with patch.dict('os.environ', values, clear=False):
            env = handoff._base_env(git_executable())
        self.assertFalse(any(key.startswith('GIT_TRACE') for key in env))
        for key in values:
            self.assertNotIn(key, env)
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')


if __name__ == '__main__':
    unittest.main()
