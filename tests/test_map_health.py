import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from audit_map_health import health_audit, main, write_health_status
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

    def test_workflows_persist_before_artifact_and_use_current_notification_snapshot(self):
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        health = (workflows / 'weekly-map-validation.yml').read_text()
        notifications = (workflows / 'location-alerts.yml').read_text()
        self.assertIn('if: always() && steps.audit.outcome', health)
        self.assertLess(health.index('write_health_status('), health.index('actions/upload-artifact'))
        self.assertNotIn('issues.update', health)
        self.assertIn("ref: main", notifications)
        self.assertIn("load_health_status('map-health-status.json')", notifications)
        self.assertIn('report.health_count || failed', notifications)
        self.assertIn('const representedHealth = isHealth && (newerHealth || (sameHealthAttempt && report.health_count))', notifications)
        self.assertIn('--report notification-report.json --failure-url "$FAILURE_URL"', notifications)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the GitHub notification script')
    def test_notification_workflow_uses_latest_health_and_keeps_unrecorded_failures_visible(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/location-alerts.yml').read_text()
        script = textwrap.dedent(workflow.split('      - name: Update the persistent GitHub review issue', 1)[1]
                                 .split('          script: |', 1)[1].split('      - name:', 1)[0])
        # Execute the actual workflow script with mocked GitHub/FS interfaces.
        cases = [
            ({'health_run_id': '11', 'health_count': 1}, {'id': 11}, '', 'open'),
            ({'health_run_id': '12', 'health_count': 0, 'health_checked_at': '2026-10-31T11:00:00Z'},
             {'id': 11, 'updated_at': '2026-10-31T10:00:00Z'}, '', 'closed'),
            ({'health_run_id': '11', 'health_run_attempt': 2, 'health_count': 0},
             {'id': 11, 'run_attempt': 1}, '', 'closed'),
            ({'health_run_id': '11', 'health_count': 0}, {'id': 11}, 'https://example.test/run', 'open'),
            ({'health_run_id': '10', 'health_count': 0}, {'id': 11}, 'https://example.test/run', 'open'),
            # A fresh rerun of an older ID supersedes a delayed higher-ID event.
            ({'health_run_id': '10', 'health_count': 0, 'health_checked_at': '2026-10-31T11:00:00Z'},
             {'id': 11, 'updated_at': '2026-10-31T10:00:00Z'}, '', 'closed'),
            # A newer actual failure of an older run must remain visible.
            ({'health_run_id': '11', 'health_count': 0, 'health_checked_at': '2026-10-31T10:00:00Z'},
             {'id': 10, 'run_attempt': 2, 'updated_at': '2026-10-31T11:00:00Z'}, 'https://example.test/run', 'open'),
            # Missing/invalid timestamps cannot prove cross-run supersession.
            ({'health_run_id': '12', 'health_count': 0, 'health_checked_at': 'invalid'},
             {'id': 11, 'updated_at': '2026-10-31T11:00:00Z'}, 'https://example.test/run', 'open'),
            ({'health_run_id': '12', 'health_count': 0},
             {'id': 11}, 'https://example.test/run', 'open'),
            # Same-ID ordering uses attempts even when timestamps suggest otherwise.
            ({'health_run_id': '11', 'health_run_attempt': 2, 'health_count': 0, 'health_checked_at': '2026-10-31T10:00:00Z'},
             {'id': 11, 'run_attempt': 1, 'updated_at': '2026-10-31T11:00:00Z'}, '', 'closed'),
            ({'health_run_id': '11', 'health_run_attempt': 1, 'health_count': 1},
             {'id': 11, 'run_attempt': 2}, 'https://example.test/run', 'open'),
        ]
        harness = '''
        const {script, cases} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
        (async () => {
          const outputs = [];
          for (const [snapshot, event] of cases) {
            const updates = [], exported = {};
            const report = {...snapshot, active_count: 0};
            const fs = {readFileSync: path => path.endsWith('.json') ? JSON.stringify(report) : 'Current report'};
            const github = {rest: {issues: {
              get: async () => ({data: {number: 5, state: 'open', body: 'Old'}}),
              update: async args => updates.push(args)
            }}};
            const context = {repo: {owner: 'test', repo: 'test'}, payload: {workflow_run: {
              name: 'Daily map validation', conclusion: 'failure', html_url: 'https://example.test/run', ...event
            }}};
            await new AsyncFunction('require', 'github', 'context', 'core', script)(
              () => fs, github, context, {exportVariable: (key, value) => exported[key] = value});
            outputs.push([exported.FAILURE_URL, updates[0].state]);
          }
          process.stdout.write(JSON.stringify(outputs));
        })().catch(error => { console.error(error); process.exit(1); });
        '''
        result = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps(
            dict(script=script, cases=[[snapshot, event] for snapshot, event, _, _ in cases])),
            text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [[url, state] for _, _, url, state in cases])


if __name__ == '__main__':
    unittest.main()
