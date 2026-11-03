import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from directory_release import read_json
from hermes_google_runner import atomic_json, execute, fetch_inputs, validate_inputs

ROOT = Path(__file__).resolve().parents[1]


class HermesRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = read_json(ROOT / 'directory-release.json')
        cls.registry = read_json(ROOT / 'venue-registry.json')

    def test_tampered_release_or_mismatched_registry_is_rejected(self):
        changed = copy.deepcopy(self.bundle)
        changed['rows'][0]['pub_name'] = 'Different pub'
        with self.assertRaisesRegex(ValueError, 'Invalid published release'):
            validate_inputs(changed, self.registry)
        registry = copy.deepcopy(self.registry)
        identifier = self.bundle['rows'][0]['venue_id']
        registry['venues'][identifier]['verified_pin']['lat'] += .001
        with self.assertRaisesRegex(ValueError, 'points differ'):
            validate_inputs(self.bundle, registry)

    def test_downloads_are_bound_to_one_commit(self):
        urls = []
        commit = 'a' * 40
        def lookup(url):
            urls.append(url)
            if '/git/ref/' in url:
                return {'object': {'sha': commit}}
            return self.registry if url.endswith('venue-registry.json') else self.bundle
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(fetch_inputs(Path(temp), lookup), commit)
        self.assertEqual(len(urls), 3)
        self.assertTrue(all('/' + commit + '/' in url for url in urls[1:]))

    def stage(self, state):
        data = state / 'data'
        atomic_json(data / 'directory-release.json', self.bundle)
        atomic_json(data / 'venue-registry.json', self.registry)

    def test_failed_browser_preserves_previous_status_and_has_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ):
            state = Path(temp)
            self.stage(state)
            atomic_json(state / 'hermes-google-status.json', {'previous': 'accepted status'})
            def failing(*args, **kwargs):
                raise RuntimeError('Provider text that must not enter the receipt')
            with self.assertRaisesRegex(RuntimeError, 'comparison failed'):
                execute(state, fetch=False, browser_run=failing)
            self.assertEqual(read_json(state / 'hermes-google-status.json'), {'previous': 'accepted status'})
            receipt = read_json(state / 'last-run.json')
            self.assertEqual(receipt['state'], 'failed')
            self.assertEqual(receipt['failure_code'], 'RuntimeError')
            self.assertNotIn('Provider text', str(receipt))
            self.assertFalse((state / 'runner.lock').exists())

    def test_execution_receipt_binds_output_and_empty_checks_stay_incomplete(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ):
            state = Path(temp)
            self.stage(state)
            def blocked(root, receipts, **kwargs):
                atomic_json(receipts, {'schema_version': 1, 'observations': []})
                return {'attempted': 0, 'stopped': True}
            receipt = execute(state, fetch=False, browser_run=blocked)
            status_path = state / 'hermes-google-status.json'
            self.assertEqual(receipt['state'], 'blocked')
            self.assertEqual(receipt['status_sha256'], hashlib.sha256(status_path.read_bytes()).hexdigest())
            status = read_json(status_path)
            self.assertEqual(status['counts']['current'], 0)
            self.assertGreater(status['counts']['unchecked_or_stale'], 0)
            self.assertEqual(status['run_id'], receipt['execution_id'])


if __name__ == '__main__':
    unittest.main()
