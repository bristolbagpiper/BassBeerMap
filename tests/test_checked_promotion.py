import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from promote_checked_commit import promote


class CheckedPromotionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repo = root / 'source'
        self.remote = root / 'remote.git'
        self.repo.mkdir()
        self.run_git('init', '-b', 'main')
        self.run_git('config', 'user.name', 'Release test')
        self.run_git('config', 'user.email', 'release@example.invalid')
        self.run_git('init', '--bare', str(self.remote))
        self.run_git('remote', 'add', 'origin', str(self.remote))
        (self.repo / 'pubs.csv').write_text('old\n', encoding='utf-8')
        self.run_git('add', 'pubs.csv')
        self.run_git('commit', '-m', 'published')
        self.base = self.run_git('rev-parse', 'HEAD')
        self.run_git('push', 'origin', 'HEAD:main')
        (self.repo / 'pubs.csv').write_text('reviewed\n', encoding='utf-8')
        self.run_git('commit', '-am', 'reviewed directory')
        self.candidate = self.run_git('rev-parse', 'HEAD')
        self.executions = []
        self.outcome = 'success'
        self.result_sha = self.candidate

    def run_git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.repo, capture_output=True, check=True,
                                env=dict(os.environ))
        return result.stdout.decode().rstrip('\r\n')

    def api(self, url, **kwargs):
        if kwargs.get('method') == 'POST':
            self.assertTrue(url.endswith('/workflows/validate-coordinates.yml/dispatches'))
            self.assertEqual(json.loads(kwargs['data']), {'ref': 'codex/validated-17-1'})
            ref = self.run_git('ls-remote', 'origin', 'refs/heads/codex/validated-17-1')
            self.assertEqual(ref.split()[0], self.candidate)
            self.executions.append(107)
            return {'workflow_run_id': 107}
        self.assertTrue(url.endswith('/runs/107'))
        return dict(status='completed', conclusion=self.outcome, head_sha=self.result_sha,
                    event='workflow_dispatch', head_branch='codex/validated-17-1')

    def publish(self):
        return promote('example/map', 'local-fixture', '17-1', run=self.run_git, request=self.api)

    def test_exact_candidate_reaches_main_once_and_ephemeral_ref_is_removed(self):
        self.assertEqual(self.publish(), self.candidate)
        self.assertEqual(self.run_git('ls-remote', 'origin', 'refs/heads/main').split()[0], self.candidate)
        self.assertEqual(self.run_git('ls-remote', 'origin', 'refs/heads/codex/*'), '')
        self.assertEqual(self.publish(), self.candidate)
        self.assertEqual(self.executions, [107])

    def test_unsuccessful_or_different_commit_is_never_promoted(self):
        for outcome, sha in [('failure', self.candidate), ('success', self.base)]:
            with self.subTest(outcome=outcome, sha=sha):
                self.outcome, self.result_sha = outcome, sha
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.run_git('ls-remote', 'origin', 'refs/heads/main').split()[0], self.base)
                self.assertEqual(self.run_git('ls-remote', 'origin', 'refs/heads/codex/*'), '')

    def test_code_and_stale_parent_cannot_use_automatic_data_promotion(self):
        (self.repo / 'code.py').write_text('pass\n', encoding='utf-8')
        self.run_git('add', 'code.py')
        self.run_git('commit', '-m', 'code change')
        with self.assertRaisesRegex(ValueError, 'data files'):
            self.publish()
        (self.repo / 'pubs.csv').write_text('newer\n', encoding='utf-8')
        self.run_git('commit', '-am', 'commit with unpublished parent')
        with self.assertRaisesRegex(ValueError, 'main advanced'):
            self.publish()
        self.assertEqual(self.executions, [])
