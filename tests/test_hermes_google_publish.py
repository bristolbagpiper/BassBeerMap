import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from build_place_review_queue import build_queue
from directory_release import read_json, write_json
from hermes_google_publish import (
    BLOCKED_ISSUE,
    FAILED_ISSUE,
    prepare_candidate,
    write_candidate,
)
from hermes_google_review import build_status
from hermes_google_runner import validate_inputs


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
CURRENT_COMMIT = 'b' * 40
SOURCE_COMMIT = 'a' * 40


def write_exact_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def release_digest(bundle):
    payload = {key: value for key, value in bundle.items() if key != 'release_id'}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class HermesGooglePublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = read_json(ROOT / 'directory-release.json')
        cls.registry = read_json(ROOT / 'venue-registry.json')
        cls.item = build_queue(cls.bundle, cls.registry, {})[0]

    def make_case(self, state='complete'):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        state_dir = root / 'state'
        source_data = state_dir / 'data'
        source_data.mkdir(parents=True)
        write_exact_json(source_data / 'directory-release.json', self.bundle)
        write_exact_json(source_data / 'venue-registry.json', self.registry)
        write_exact_json(source_data / 'observations.json', {'schema_version': 1, 'observations': []})

        started = '2026-11-12T11:58:00Z'
        finished = '2026-11-12T11:59:00Z'
        checked_at = '2026-11-12T11:58:30Z'
        attempted_at = '2026-11-12T11:58:10Z'
        observation = {
            'schema_version': 1,
            'provider': 'hermes-browser',
            'venue_id': self.item['venue_id'],
            'identity_signature': self.item['identity_signature'],
            'point_signature': self.item['point_signature'],
            'binding_signature': self.item['binding_signature'],
            'category': 'trading_conflict',
            'attempted_at': attempted_at,
            'checked_at': checked_at,
            'result_url': 'https://www.google.com/maps/place/SECRET-GOOGLE-PLACE/',
            'source_url': '',
            'business_status': 'CLOSED_TEMPORARILY',
        }
        receipts = {'schema_version': 1, 'observations': [observation]}
        batch = {'selected': 1, 'attempted': 1, 'checked': 1, 'ambiguous': 0, 'unavailable': 0}
        status = build_status(source_data, receipts, now=datetime.fromisoformat(checked_at.replace('Z', '+00:00')),
                              batch=batch)
        execution_id = 'c' * 32
        status.update(provider='hermes-browser', run_id=execution_id, run_attempt=1, run_url='',
                      batch=batch, source_execution_finished_at=finished)
        status_raw = (json.dumps(status, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        (state_dir / 'hermes-google-status.json').write_bytes(status_raw)
        observation_raw = (json.dumps(receipts, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        (state_dir / 'observations.json').write_bytes(observation_raw)
        execution = {
            'schema_version': 1,
            'provider': 'hermes-browser',
            'execution_id': execution_id,
            'started_at': started,
            'finished_at': finished,
            'state': state,
            'source_commit': SOURCE_COMMIT,
            'release_id': self.bundle['release_id'],
            'observations_written': 1,
            'batch': batch,
            'observations_sha256': hashlib.sha256(observation_raw).hexdigest(),
            'status_sha256': hashlib.sha256(status_raw).hexdigest(),
            'detail': 'PRIVATE DIAGNOSTIC: raw browser failure text and user@example.test',
        }
        if state == 'blocked':
            execution['batch'] = dict(batch, selected=2)
            # The local status records the exact partial batch written by the run.
            status['batch'] = execution['batch']
            status_raw = (json.dumps(status, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            (state_dir / 'hermes-google-status.json').write_bytes(status_raw)
            execution['status_sha256'] = hashlib.sha256(status_raw).hexdigest()
        if state == 'failed':
            execution = {
                'schema_version': 1, 'execution_id': execution_id,
                'started_at': started, 'finished_at': finished, 'state': 'failed',
                'failure_code': 'RuntimeError',
                'detail': 'PRIVATE FAILURE DETAIL user@example.test https://google.com/private',
            }
        write_exact_json(state_dir / 'last-run.json', execution)
        return temporary, state_dir, source_data, execution

    def test_complete_candidate_is_bound_rebuilt_and_strictly_projected(self):
        temporary, state_dir, current_data, execution = self.make_case()
        self.addCleanup(temporary.cleanup)
        candidate = prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)
        self.assertEqual(candidate['execution_state'], 'complete')
        self.assertEqual(candidate['run_id'], execution['execution_id'])
        self.assertEqual(candidate['source_commit'], SOURCE_COMMIT)
        self.assertEqual(candidate['source_release_id'], self.bundle['release_id'])
        self.assertEqual(candidate['validated_release_id'], self.bundle['release_id'])
        self.assertEqual(candidate['source_status_sha256'], execution['status_sha256'])
        self.assertEqual(len(candidate['findings']), 1)
        self.assertEqual(set(candidate['findings'][0]), {
            'venue_id', 'pub_name', 'place_name', 'postcode', 'status', 'reason', 'binding_signature'
        })
        rendered = json.dumps(candidate)
        for forbidden in ('SECRET-GOOGLE-PLACE', 'google.com/maps', 'user@example.test',
                          'observations', 'current_pin', 'business_status'):
            self.assertNotIn(forbidden, rendered)

    def test_different_current_point_rebuilds_away_stale_finding(self):
        temporary, state_dir, source_data, _ = self.make_case()
        self.addCleanup(temporary.cleanup)
        current_data = Path(temporary.name) / 'current'
        current_data.mkdir()
        changed_bundle = copy.deepcopy(self.bundle)
        changed_registry = copy.deepcopy(self.registry)
        identifier = self.item['venue_id']
        changed_bundle['locations'][identifier]['lat'] += 0.001
        changed_registry['venues'][identifier]['verified_pin']['lat'] += 0.001
        changed_bundle['release_id'] = release_digest(changed_bundle)
        validate_inputs(changed_bundle, changed_registry)
        write_exact_json(current_data / 'directory-release.json', changed_bundle)
        write_exact_json(current_data / 'venue-registry.json', changed_registry)
        candidate = prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)
        self.assertNotEqual(candidate['source_release_id'], candidate['validated_release_id'])
        self.assertEqual(candidate['findings'], [])
        self.assertEqual(candidate['counts']['current'], 0)
        self.assertIn('incomplete', candidate['issues'][0].lower())

    def test_blocked_run_is_diagnostic_only_on_the_handoff(self):
        temporary, state_dir, current_data, _ = self.make_case(state='blocked')
        self.addCleanup(temporary.cleanup)
        candidate = prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)
        self.assertEqual(candidate['execution_state'], 'blocked')
        self.assertEqual(candidate['findings'], [])
        self.assertEqual(candidate['counts']['current'], 0)
        self.assertEqual(candidate['counts']['unchecked_or_stale'], candidate['counts']['total'])
        self.assertIn(BLOCKED_ISSUE, candidate['issues'])

    def test_failed_run_is_diagnostic_only_and_does_not_copy_prior_status(self):
        temporary, state_dir, current_data, _ = self.make_case(state='failed')
        self.addCleanup(temporary.cleanup)
        candidate = prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)
        self.assertEqual(candidate['execution_state'], 'failed')
        self.assertEqual(candidate['source_commit'], None)
        self.assertEqual(candidate['source_release_id'], None)
        self.assertEqual(candidate['source_status_sha256'], None)
        self.assertEqual(candidate['findings'], [])
        self.assertEqual(candidate['batch']['attempted'], 0)
        self.assertIn(FAILED_ISSUE, candidate['issues'])
        rendered = json.dumps(candidate)
        self.assertNotIn('PRIVATE FAILURE DETAIL', rendered)
        self.assertNotIn('user@example.test', rendered)
        self.assertNotIn('google.com/private', rendered)

    def test_status_and_journal_hashes_are_required_when_receipt_has_them(self):
        temporary, state_dir, current_data, _ = self.make_case()
        self.addCleanup(temporary.cleanup)
        path = state_dir / 'observations.json'
        path.write_text(path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'journal hash'):
            prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)

    def test_saved_status_bytes_must_match_the_execution_receipt_hash(self):
        temporary, state_dir, current_data, _ = self.make_case()
        self.addCleanup(temporary.cleanup)
        path = state_dir / 'hermes-google-status.json'
        path.write_text(path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'status hash'):
            prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)

    def test_legacy_run_without_observation_hash_remains_compatible(self):
        temporary, state_dir, current_data, execution = self.make_case()
        self.addCleanup(temporary.cleanup)
        execution.pop('observations_sha256')
        write_exact_json(state_dir / 'last-run.json', execution)
        candidate = prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)
        self.assertEqual(candidate['execution_state'], 'complete')

    def test_stale_run_and_active_lock_are_rejected(self):
        temporary, state_dir, current_data, _ = self.make_case()
        self.addCleanup(temporary.cleanup)
        stale_now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, 'stale'):
            prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=stale_now)
        (state_dir / 'runner.lock').write_text('active', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'active'):
            prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)
        (state_dir / 'runner.lock').unlink()
        (state_dir / 'observations.json.lock').write_text('active', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'active'):
            prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)

    def test_output_is_create_only_and_forbidden_inside_runtime(self):
        temporary, state_dir, current_data, _ = self.make_case()
        self.addCleanup(temporary.cleanup)
        candidate = prepare_candidate(state_dir, current_data, CURRENT_COMMIT, now=NOW)
        output = Path(temporary.name) / 'candidate.json'
        write_candidate(output, candidate, state_dir=state_dir, current_data_root=current_data)
        original = output.read_bytes()
        with self.assertRaises(FileExistsError):
            write_candidate(output, candidate, state_dir=state_dir, current_data_root=current_data)
        self.assertEqual(output.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, 'outside runtime'):
            write_candidate(state_dir / 'candidate.json', candidate,
                            state_dir=state_dir, current_data_root=current_data)
        repository_output = ROOT / 'must-not-be-created-google-handoff.json'
        with self.assertRaisesRegex(ValueError, 'outside runtime'):
            write_candidate(repository_output, candidate, state_dir=state_dir,
                            current_data_root=current_data, repository=ROOT)
        self.assertFalse(repository_output.exists())

    def test_git_repository_mode_reads_only_the_explicit_commit_objects(self):
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, check=True,
                                capture_output=True, text=True).stdout.strip()
        temporary, state_dir, _, _ = self.make_case()
        self.addCleanup(temporary.cleanup)
        # The contract under test is the exact `git show <commit>:<path>` data
        # staging helper used by CLI repository mode; no checkout/branch writes.
        from hermes_google_publish import _stage_commit_data
        staged = Path(temporary.name) / 'staged'
        staged.mkdir()
        _stage_commit_data(ROOT, commit, staged)
        self.assertEqual(read_json(staged / 'directory-release.json'), self.bundle)
        self.assertEqual(read_json(staged / 'venue-registry.json'), self.registry)


if __name__ == '__main__':
    unittest.main()
