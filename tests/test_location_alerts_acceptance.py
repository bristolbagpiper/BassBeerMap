import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from validate_google_handoff import check_handoff


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class GoogleHandoffAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write('google-review-provider.json', {'provider': 'hermes-browser'})
        self.write('hermes-google-status.json', {
            'schema_version': 1,
            'provider': 'hermes-browser',
            'configured': True,
            'directory_binding_signature': 'd' * 64,
            'issues': [],
            'findings': [],
            'counts': {'total': 5, 'current': 5, 'unchecked_or_stale': 0},
            'execution_state': 'complete',
            'hard_failure': False,
            'run_id': 'a' * 32,
            'run_attempt': 1,
            'source_commit': 'b' * 40,
            'source_release_id': 'c' * 64,
            'validated_release_id': 'e' * 64,
            'source_status_sha256': 'f' * 64,
            'checked_at': (NOW - timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
            'batch': {'selected': 0, 'attempted': 0, 'checked': 0, 'ambiguous': 0, 'unavailable': 0},
            'source_execution_finished_at': (NOW - timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
        })

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding='utf-8')

    def test_recent_successful_native_receipt_passes(self):
        self.assertEqual(check_handoff(self.root, now=NOW), [])

    def test_receipt_exactly_48_hours_old_passes(self):
        self.write('hermes-google-status.json', {
            **json.loads((self.root / 'hermes-google-status.json').read_text(encoding='utf-8')),
            'source_execution_finished_at': (NOW - timedelta(hours=48)).isoformat(),
            'checked_at': (NOW - timedelta(hours=48)).isoformat(),
        })
        self.assertEqual(check_handoff(self.root, now=NOW), [])

    def test_missing_status_fails(self):
        (self.root / 'hermes-google-status.json').unlink()
        self.assertTrue(any('missing or invalid' in failure for failure in check_handoff(self.root, now=NOW)))

    def test_provider_change_cannot_disable_required_native_provider(self):
        self.write('google-review-provider.json', {'provider': 'places-api'})
        self.assertTrue(any('expected exact configured provider hermes-browser' in failure
                            for failure in check_handoff(self.root, now=NOW)))

    def test_unexpected_status_provider_fails(self):
        status = json.loads((self.root / 'hermes-google-status.json').read_text(encoding='utf-8'))
        status['provider'] = 'places-api'
        self.write('hermes-google-status.json', status)
        self.assertTrue(any('expected status provider hermes-browser' in failure
                            for failure in check_handoff(self.root, now=NOW)))

    def test_stale_failed_and_blocked_source_runs_fail(self):
        for changes, expected in (
            ({'source_execution_finished_at': (NOW - timedelta(hours=48, seconds=1)).isoformat()}, 'older than 48 hours'),
            ({'execution_state': 'failed'}, 'not complete'),
            ({'execution_state': 'blocked'}, 'not complete'),
            ({'hard_failure': True}, 'hard failure'),
        ):
            with self.subTest(expected=expected):
                status = json.loads((self.root / 'hermes-google-status.json').read_text(encoding='utf-8'))
                status.update(changes)
                if 'source_execution_finished_at' in changes:
                    status['checked_at'] = changes['source_execution_finished_at']
                self.write('hermes-google-status.json', status)
                failures = check_handoff(self.root, now=NOW)
                self.assertTrue(any(expected in failure for failure in failures), failures)

    def test_missing_or_future_completion_time_fails(self):
        for value in (None, (NOW + timedelta(minutes=1)).isoformat()):
            with self.subTest(value=value):
                status = json.loads((self.root / 'hermes-google-status.json').read_text(encoding='utf-8'))
                status['source_execution_finished_at'] = value
                self.write('hermes-google-status.json', status)
                failures = check_handoff(self.root, now=NOW)
                self.assertTrue(failures)

    def test_structurally_incomplete_or_inconsistent_status_fails(self):
        for field, value in (('run_id', 17), ('counts', {'total': 4, 'current': 5, 'unchecked_or_stale': 0}),
                             ('batch', {'selected': 2}), ('source_status_sha256', 'not-a-hash')):
            with self.subTest(field=field):
                status = json.loads((self.root / 'hermes-google-status.json').read_text(encoding='utf-8'))
                status[field] = value
                self.write('hermes-google-status.json', status)
                self.assertTrue(check_handoff(self.root, now=NOW))

    def test_extra_provider_configuration_fields_fail(self):
        self.write('google-review-provider.json', {'provider': 'hermes-browser', 'fallback': 'places-api'})
        self.assertTrue(check_handoff(self.root, now=NOW))

    def test_existing_routes_and_reporting_stay_before_acceptance_gate(self):
        workflow = Path(__file__).resolve().parents[1] / '.github/workflows/location-alerts.yml'
        source = workflow.read_text(encoding='utf-8')
        self.assertIn('  workflow_run:', source)
        self.assertIn('  push:', source)
        self.assertIn("'validate_google_handoff.py'", source.split('    paths:', 1)[1].split('\n', 1)[0])
        self.assertIn("if: github.actor != 'github-actions[bot]' || github.event_name == 'workflow_run'", source)
        report = source.index('      - name: Update the persistent GitHub review issue')
        email = source.index('      - name: Email changed unverified locations and failed checks')
        failure_issue = source.index('      - name: Make notification failures visible')
        gate = source.index('      - name: Enforce native Hermes Google handoff acceptance')
        self.assertLess(report, email)
        self.assertLess(email, failure_issue)
        self.assertLess(failure_issue, gate)
        self.assertIn('        if: always()\n        run: python validate_google_handoff.py', source[gate:])


if __name__ == '__main__':
    unittest.main()
