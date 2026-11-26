import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audit_map_health import health_audit, integrity_check, main, write_health_status
from directory_release import read_json, write_json, write_release
from location_review import with_health
from tests.test_directory_release import pin, registry_for, row


class MapHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'published'
        self.stage = Path(self.temporary.name) / 'candidate'
        self.rows = [row(f'Example Inn {i}') for i in range(20)]
        self.registry = registry_for(self.rows)
        write_release(self.root, self.rows, self.registry, {}, {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}})
        self.before = {path.name: path.read_bytes() for path in self.root.iterdir()}

    def audit(self, live):
        context = patch('audit_map_health.fetch_text', side_effect=live) if isinstance(live, Exception) else patch(
            'audit_map_health.fetch_text', return_value=json.dumps(live))
        with context:
            report = health_audit(self.root, self.stage, limit=0)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, self.before)
        return report

    def test_wrong_release_is_unhealthy_even_with_valid_unchanged_pins(self):
        report = self.audit({'schema_version': 1, 'release_id': 'old-release'})
        self.assertTrue(report['valid'])
        self.assertFalse(report['healthy'])
        self.assertIn('not serving the committed release', report['live_issues'][0])
        self.assertEqual(read_json(self.stage / 'venue-registry.json')['venues'], self.registry['venues'])
        with patch('audit_map_health.health_audit', return_value=report), patch('builtins.print'):
            self.assertEqual(main([]), 1)

    def test_integrity_check_is_read_only_and_never_rechecks_source_evidence(self):
        live = read_json(self.root / 'directory-release.json')
        with patch('audit_map_health.fetch_text', return_value=json.dumps(live)), patch(
                'audit_map_health.recheck_pin', side_effect=AssertionError('source checks are out of scope')):
            report = integrity_check(self.root)
            self.assertEqual(main(['--root', str(self.root), '--staging', str(self.stage), '--integrity-only']), 0)
        self.assertTrue(report['healthy'], report)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, self.before)
        self.assertFalse(self.stage.exists())

    def test_integrity_check_fails_on_live_release_mismatch_or_unreachable_endpoint(self):
        live = read_json(self.root / 'directory-release.json')
        live['release_id'] = 'old-release'
        with patch('audit_map_health.fetch_text', return_value=json.dumps(live)):
            report = integrity_check(self.root)
        self.assertFalse(report['healthy'])
        self.assertIn('not serving the committed release', report['live_issues'][0])
        with patch('audit_map_health.fetch_text', side_effect=TimeoutError()):
            self.assertFalse(integrity_check(self.root)['healthy'])

    def test_live_unreachable_and_invalid_schema_are_actionable(self):
        report = self.audit(TimeoutError())
        self.assertFalse(report['healthy'])
        self.assertIn('TimeoutError', report['live_issues'][0])
        live = read_json(self.root / 'directory-release.json')
        live['schema_version'] = 99
        report = self.audit(live)
        self.assertIn('schema is invalid', report['live_issues'][0])

    def test_recovery_replaces_failed_status_without_modifying_venue_review(self):
        self.audit(TimeoutError())
        target = Path(self.temporary.name) / 'status.json'
        failed = write_health_status(self.stage, target, '10', 'https://example.test/10')
        self.assertTrue(failed['issues'])
        report = self.audit(read_json(self.root / 'directory-release.json'))
        self.assertTrue(report['healthy'])
        recovered = write_health_status(self.stage, target, '11', 'https://example.test/11')
        self.assertEqual(recovered['issues'], [])
        self.assertEqual(read_json(target)['run_id'], '11')
        queue = read_json(self.root / 'location-review.json')
        self.assertEqual(with_health(queue, recovered)['records'], queue['records'])

    def test_missing_report_and_validation_failure_are_durable(self):
        target = Path(self.temporary.name) / 'status.json'
        status = write_health_status(self.stage, target, '1')
        self.assertIn('before producing', status['issues'][0])
        self.stage.mkdir(exist_ok=True)
        write_json(self.stage / 'health-report.json', {'errors': ['Invalid ledger'], 'live_issues': []})
        self.assertEqual(write_health_status(self.stage, target)['issues'], ['Invalid ledger'])
        write_json(self.stage / 'health-report.json', {'errors': [], 'live_issues': []})
        self.assertIn('pipeline failed', write_health_status(self.stage, target, pipeline_failed=True)['issues'][0])

    def test_source_outage_is_queued_and_recovery_clears_it_without_moving_pin(self):
        with patch('audit_map_health.recheck_pin', side_effect=TimeoutError()), patch('audit_map_health.time.sleep'):
            report = health_audit(self.root, self.stage, limit=1, check_live=False)
        review = read_json(self.stage / 'location-review.json')
        self.assertTrue(report['source_warnings'])
        active = [record for record in review['records'].values() if record['active']]
        self.assertEqual(len(active), 1)
        self.assertTrue(any('Verification source unavailable' in reason for reason in active[0]['reasons']))
        after = read_json(self.stage / 'venue-registry.json')
        identifier = active[0]['venue_id']
        self.assertEqual(after['venues'][identifier]['verified_pin'], self.registry['venues'][identifier]['verified_pin'])
        # Check the same venue again, independently of the normal rotation.
        recovered_stage = Path(self.temporary.name) / 'recovered'
        checked_row = next(row for row in self.rows if row['venue_id'] == identifier)
        with patch('audit_map_health.select_batch', return_value=[checked_row]), patch('audit_map_health.recheck_pin',
                return_value={'status': 'confirmed', 'evidence': pin()['evidence'][0]}), patch('audit_map_health.time.sleep'):
            health_audit(self.stage, recovered_stage, limit=1, check_live=False)
        self.assertEqual(read_json(recovered_stage / 'location-review.json')['active_count'], 0)

    def test_unresolved_source_outage_preserves_diagnostics_and_queues_failure(self):
        identifier = self.rows[0]['venue_id']
        del self.registry['venues'][identifier]['verified_pin']
        write_release(self.root, self.rows, self.registry, {}, {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}})
        def outage(row, diagnostics, *, centre=None):
            diagnostics['sources'] = {'fsa': {'status': 'unavailable'}}
            raise TimeoutError()
        with patch('audit_map_health.resolve_new_pin', side_effect=outage), patch('audit_map_health.time.sleep'):
            health_audit(self.root, self.stage, limit=1, check_live=False)
        record = read_json(self.stage / 'location-review.json')['records'][identifier]
        self.assertTrue(any('Verification source unavailable' in reason for reason in record['reasons']))
        self.assertEqual(record['source_checks']['fsa']['status'], 'unavailable')

    def test_accepted_postcode_and_fsa_differences_do_not_become_health_alerts(self):
        identifier = self.rows[0]['venue_id']
        entry = self.registry['venues'][identifier]
        issue = dict(listed='BS1 1AA', reference='BS1 1AB', source_url='https://camra.org.uk/pubs/example-123')
        issue['owner_review'] = dict(checked_at='2026-10-31', reviewed_by='owner', owner_instruction='accepted',
            pub_name=self.rows[0]['pub_name'], listed=issue['listed'], reference=issue['reference'],
            source_url=issue['source_url'], approved_coordinate={'lat': 51.45, 'lng': -2.59})
        entry['verified_pin']['metadata_issue'] = issue
        entry['independent_location_check'] = dict(status='coordinate_difference', checked_at='2026-10-31',
            pin={'lat': 51.45, 'lng': -2.59}, listing={key: self.rows[0][key] for key in ('pub_name', 'postcode')})
        write_release(self.root, self.rows, self.registry, {}, {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}})
        report = health_audit(self.root, self.stage, limit=0, check_live=False)
        self.assertEqual(report['issues'], [])
        self.assertFalse(report['notify'])

    def test_workflows_keep_health_audit_separate_from_owner_question_notifications(self):
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        health = (workflows / 'weekly-map-validation.yml').read_text()
        notifications = (workflows / 'location-alerts.yml').read_text()
        self.assertIn('python audit_map_health.py --integrity-only', health)
        self.assertIn('contents: read', health)
        self.assertNotIn('workflow_run:', health)
        self.assertNotIn('audit_google_comparisons.py', health)
        self.assertNotIn('full_location_audit.py', health)
        self.assertNotIn('git add', health)
        self.assertNotIn('git push', health)
        self.assertNotIn('coordinate-health-state.json', health)
        self.assertNotIn('map-health-status.json', health)
        self.assertIn("ref: main", notifications)
        self.assertNotIn('load_health_status', notifications)
        self.assertIn('trading-notices.json', notifications)
        self.assertNotIn('--failure-url', notifications)
        self.assertNotIn('FAILURE_URL', notifications)
        self.assertNotIn('Weekly map validation', notifications)

    def test_notification_workflow_skips_rejected_imports_and_keeps_their_issue_route(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/location-alerts.yml').read_text()
        self.assertIn("workflows: ['Update CSV From PDF', 'Apply reviewed location']", workflow)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", workflow)
        self.assertNotIn('actions.listJobsForWorkflowRunAttempt', workflow)
        import_workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/update-csv-from-pdf.yml').read_text()
        self.assertIn("const title = 'BassBeerMap update rejected'", import_workflow)
        self.assertIn('The previous published release is retained.', import_workflow)


if __name__ == '__main__':
    unittest.main()
