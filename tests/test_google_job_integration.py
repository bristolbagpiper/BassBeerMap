"""Offline checks of scheduled Google comparison persistence and notification."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import MagicMock, patch

from build_place_review_queue import digest, signatures
from location_review import (GOOGLE_CONFIGURATION_ISSUE, GOOGLE_COVERAGE_ISSUE, GOOGLE_STALE_STATUS_ISSUE, fingerprint,
                             format_report, load_google_status, with_google, with_health)
from notify_location_review import notify


ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which('git')
GIT_BASH = Path(GIT).parent.parent / 'bin/bash.exe' if GIT else Path('missing-bash')
BASH = str(GIT_BASH) if GIT_BASH.is_file() else (shutil.which('bash') if os.name != 'nt' else None)


class GoogleJobIntegrationTests(unittest.TestCase):
    def bundle(self):
        return dict(rows=[dict(venue_id='venue-test', pub_name='Directory Pub', place_name='Directory Town',
                              postcode='BS1 1AA')],
                    locations={'venue-test': dict(lat=51.45, lng=-2.58, precision='venue')}, release_id='d' * 64)

    def attach(self, report, status):
        return with_google(report, status, self.bundle(), now=datetime(2026, 11, 2, 12, tzinfo=timezone.utc))

    def status(self, **changes):
        binding = signatures(self.bundle()['rows'][0], self.bundle()['locations']['venue-test'])['binding_signature']
        return dict(dict(schema_version=1, provider='hermes-browser', configured=True, checked_at='2026-11-02T10:00:00Z',
                         run_id='10', run_attempt='1', run_url='https://example.test/run/10',
                         source_commit='a' * 40, source_release_id='b' * 64,
                         validated_release_id=self.bundle()['release_id'], source_status_sha256='c' * 64,
                         execution_state='complete', source_execution_finished_at='2026-11-02T10:00:00Z',
                         batch=dict(selected=1, attempted=1, checked=1),
                         directory_binding_signature=digest([('venue-test', binding)]),
                         counts={'total': 1, 'current': 1, 'unchecked_or_stale': 0,
                                 'checked_this_run': 1, 'review_count': 0}, issues=[], findings=[]), **changes)

    def report(self, **changes):
        return dict(dict(records={}, active_count=0, unverified_count=0), **changes)

    def finding(self, **changes):
        binding = signatures(self.bundle()['rows'][0], self.bundle()['locations']['venue-test'])['binding_signature']
        return dict(dict(venue_id='venue-test', pub_name='Directory Pub', place_name='Directory Town',
                         postcode='BS1 1AA', status='needs_review', reason='Google comparison needs review',
                         binding_signature=binding), **changes)

    def test_google_findings_keep_separate_health_and_venue_lifecycle(self):
        original = self.report()
        health = with_health(original, dict(schema_version=1, issues=['Live map unavailable'], run_id='10'))
        report = self.attach(health, self.status(findings=[self.finding()]))
        self.assertEqual(report['health_issues'], ['Live map unavailable'])
        self.assertEqual(report['records'], original['records'])
        self.assertNotIn('google_count', health)
        self.assertEqual(report['google_count'], 1)
        text = format_report(report)
        self.assertIn('Directory Pub', text)
        self.assertIn('Google comparison needs review', text)
        self.assertIn('do not approve or move map pins', text)

    def test_missing_snapshot_reports_incomplete_coverage_without_venue_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'google.json'
            self.assertIsNone(load_google_status(path))
            for content in ('null', 'broken json'):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_google_status(path)
        report = self.attach(self.report(), None)
        self.assertEqual(report['records'], {})
        self.assertFalse(report['google_configured'])
        self.assertEqual(report['google_actionable_count'], 0)
        self.assertEqual(set(report['google_issues']), {GOOGLE_COVERAGE_ISSUE, GOOGLE_STALE_STATUS_ISSUE})
        self.assertIn(GOOGLE_STALE_STATUS_ISSUE, format_report(report))
        self.assertNotEqual(fingerprint(report), fingerprint(self.report()))

    def test_malformed_snapshot_fails_closed(self):
        malformed = [[], {}, self.status(schema_version=2), self.status(schema_version=True),
                     self.status(configured='false'), self.status(issues=['']), self.status(issues='error'),
                     self.status(counts={'checked': -1}), self.status(counts={'checked': True}),
                     self.status(findings=[self.finding(lat=51.1)]),
                     self.status(findings=[self.finding(reason=None)])]
        for status in malformed:
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'Invalid Google comparison'):
                self.attach(self.report(), status)

    def test_browser_receipt_is_required_and_diagnostics_never_expose_findings(self):
        legacy = self.status()
        legacy.pop('source_status_sha256')
        with self.assertRaisesRegex(ValueError, 'browser receipt'):
            self.attach(self.report(), legacy)
        blocked = self.status(execution_state='blocked', findings=[self.finding()])
        blocked.update(source_commit=None, source_release_id=None, source_status_sha256=None)
        report = self.attach(self.report(), blocked)
        self.assertEqual(report['google_findings'], [])
        self.assertEqual(report['google_counts']['current'], 0)
        self.assertIn(GOOGLE_COVERAGE_ISSUE, report['google_issues'])

    def test_release_metadata_change_is_allowed_when_current_bindings_match(self):
        report = self.attach(self.report(), self.status(validated_release_id='e' * 64))
        self.assertEqual(report['google_findings'], [])
        self.assertEqual(report['google_counts']['current'], 1)
        self.assertNotIn(GOOGLE_COVERAGE_ISSUE, report['google_issues'])

    def test_missed_daily_browser_runs_expire_after_two_days(self):
        status = self.status(findings=[self.finding()])
        report = with_google(self.report(), status, self.bundle(),
                             now=datetime(2026, 11, 4, 11, tzinfo=timezone.utc))
        self.assertEqual(report['google_findings'], [])
        self.assertEqual(report['google_counts']['current'], 0)
        self.assertIn(GOOGLE_STALE_STATUS_ISSUE, report['google_issues'])

    def test_hermes_provider_publishes_new_browser_batch_without_waiting_for_daily_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'google-review-provider.json').write_text('{"provider":"hermes-browser"}', encoding='utf-8')
            (root / 'google-places-status.json').write_text(json.dumps(self.status(run_id='old')), encoding='utf-8')
            self.assertIsNone(load_google_status(root / 'google-places-status.json'))
            (root / 'hermes-google-status.json').write_text(json.dumps(self.status(run_id='browser-new')), encoding='utf-8')
            self.assertEqual(load_google_status(root / 'google-places-status.json')['run_id'], 'browser-new')

    def test_hermes_local_mode_ignores_retained_public_google_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'google-review-provider.json').write_text('{"provider":"hermes-local"}', encoding='utf-8')
            (root / 'google-places-status.json').write_text(json.dumps(self.status(
                findings=[self.finding()], issues=['old warning'])), encoding='utf-8')
            (root / 'hermes-google-status.json').write_text(json.dumps(self.status(
                findings=[self.finding()], issues=['old warning'])), encoding='utf-8')

            status = load_google_status(root / 'google-places-status.json')
            attached = self.attach(self.report(), status)

        self.assertEqual(attached, self.report())
        self.assertEqual(fingerprint(attached), fingerprint(self.report()))
        self.assertNotIn('Google', format_report(attached))

    def test_google_changes_send_once_and_recovery_once_with_mock_smtp(self):
        healthy = self.attach(self.report(), self.status())
        failed = self.attach(self.report(), self.status(findings=[self.finding()]))
        settings = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test',
                        ALERT_TO='test@example.test')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, settings, clear=True):
            state = Path(directory) / 'delivery.json'
            smtp = MagicMock()
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertFalse(notify(healthy, state, smtp_factory=smtp))
            self.assertTrue(notify(failed, state, smtp_factory=smtp))
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertIn('1 Google pub actions', message['Subject'])
            later = self.attach(self.report(), self.status(findings=[self.finding()], run_id='11',
                                checked_at='2026-11-02T11:00:00Z', source_execution_finished_at='2026-11-02T11:00:00Z',
                                run_url='https://example.test/run/11'))
            self.assertFalse(notify(later, state, smtp_factory=smtp))
            changed = self.attach(self.report(), self.status(findings=[self.finding(reason='New review reason')]))
            self.assertTrue(notify(changed, state, smtp_factory=smtp))
            self.assertTrue(notify(healthy, state, smtp_factory=smtp))
            self.assertFalse(notify(healthy, state, smtp_factory=smtp))
            self.assertEqual(smtp.call_count, 3)

    def test_pipeline_failure_suppression_is_stable_and_recovery_is_one_transition(self):
        report = self.attach(self.report(), self.status())
        settings = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test',
                        ALERT_TO='test@example.test')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, settings, clear=True):
            state = Path(directory) / 'delivery.json'
            smtp = MagicMock()
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(notify(report, state, 'https://example.test/run/1', smtp_factory=smtp))
            self.assertFalse(notify(report, state, 'https://example.test/run/2', smtp_factory=smtp))
            self.assertTrue(notify(report, state, smtp_factory=smtp))
            self.assertFalse(notify(report, state, smtp_factory=smtp))
            self.assertEqual(smtp.call_count, 2)

    def test_configuration_issue_alerts_without_active_venue_or_health_reviews(self):
        issue = GOOGLE_CONFIGURATION_ISSUE
        report = self.attach(self.report(), self.status(configured=False, issues=[issue]))
        self.assertEqual(report['google_count'], 2)
        self.assertIn(issue, format_report(report))
        later = copy.deepcopy(report)
        later.update(google_run_id='15', google_checked_at='2026-11-04')
        self.assertEqual(fingerprint(report), fingerprint(later))
        self.assertNotEqual(fingerprint(report), fingerprint(self.report()))

    def test_disabled_snapshot_cannot_claim_complete_google_coverage(self):
        report = self.attach(self.report(), self.status(configured=False, issues=[],
                             counts=dict(current=0, total=1)))
        self.assertEqual(set(report['google_issues']), {GOOGLE_CONFIGURATION_ISSUE, GOOGLE_COVERAGE_ISSUE})
        self.assertEqual(report['google_actionable_count'], 0)
        self.assertIn('Google comparisons current for this listing and pin: 0/1', format_report(report))

    def test_changed_identity_point_or_removed_pub_invalidates_old_findings_at_notification_time(self):
        status = self.status(findings=[self.finding()])
        changed_bundles = []
        for key, value in (('pub_name', 'Renamed Pub'), ('postcode', 'BS1 1AB'), ('area', 'New area'), ('country', 'Wales')):
            bundle = self.bundle()
            bundle['rows'][0][key] = value
            changed_bundles.append(bundle)
        for key, value in (('lat', 51.46), ('lng', -2.57), ('precision', 'postcode')):
            bundle = self.bundle()
            bundle['locations']['venue-test'][key] = value
            changed_bundles.append(bundle)
        changed_bundles.append(dict(rows=[], locations={}))
        for bundle in changed_bundles:
            with self.subTest(bundle=bundle):
                report = with_google(self.report(), status, bundle)
                self.assertEqual(report['google_findings'], [])
                self.assertIn(GOOGLE_COVERAGE_ISSUE, report['google_issues'])
                self.assertNotIn('Directory Pub', format_report(report))
        with self.assertRaisesRegex(ValueError, 'Current release is required'):
            with_google(self.report(), status)

    def test_directory_binding_invalidates_previously_consistent_pub_without_findings(self):
        status = self.status()
        self.assertEqual(self.attach(self.report(), status)['google_count'], 0)
        bundle = self.bundle()
        bundle['locations']['venue-test']['lat'] += 0.01
        moved = with_google(self.report(), status, bundle)
        self.assertIn(GOOGLE_COVERAGE_ISSUE, moved['google_issues'])
        bundle = self.bundle()
        added = dict(bundle['rows'][0], venue_id='venue-new')
        bundle['rows'].append(added)
        bundle['locations']['venue-new'] = dict(bundle['locations']['venue-test'])
        self.assertIn(GOOGLE_COVERAGE_ISSUE, with_google(self.report(), status, bundle)['google_issues'])

    def test_stale_missing_or_future_status_never_claims_all_clear_and_keeps_bound_findings(self):
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        for checked_at in ('2026-09-01T10:00:00Z', None, '', 'not-a-date', '2026-11-03T10:00:00Z', '2026-11-02T10:00:00'):
            for findings in ([], [self.finding()]):
                with self.subTest(checked_at=checked_at, findings=bool(findings)):
                    report = with_google(self.report(), self.status(checked_at=checked_at, findings=findings),
                                         self.bundle(), now=now)
                    self.assertIn(GOOGLE_COVERAGE_ISSUE, report['google_issues'])
                    self.assertIn(GOOGLE_STALE_STATUS_ISSUE, report['google_issues'])
                    self.assertEqual(len(report['google_findings']), 0)
        fallback = self.status(hard_failure=True)
        fallback['execution_state'] = 'failed'
        fallback['source_commit'] = fallback['source_release_id'] = fallback['source_status_sha256'] = None
        fallback['findings'] = []
        self.assertIn(GOOGLE_COVERAGE_ISSUE, self.attach(self.report(), fallback)['google_issues'])
        for status in (self.status(directory_binding_signature='bad'), self.status(directory_binding_signature=None),
                       self.status(findings=[self.finding(binding_signature='z' * 64)])):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.attach(self.report(), status)

    def test_scheduled_job_publishes_camra_then_google_and_persists_failed_google(self):
        workflow = (ROOT / '.github/workflows/weekly-map-validation.yml').read_text()
        self.assertIn("workflows: ['Update CSV From PDF']", workflow)
        self.assertIn('types: [completed]', workflow)
        self.assertIn("if: github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success'", workflow)
        google = workflow.index('      - name: Rotate Google Places comparisons')
        persisted = workflow.index('      - name: Persist Google comparison status')
        self.assertLess(workflow.index("git commit -m 'Refresh coordinate evidence and map health'"), google)
        self.assertLess(workflow.index('write_health_status('), google)
        self.assertLess(google, persisted)
        self.assertLess(persisted, workflow.index('actions/upload-artifact'))
        self.assertIn('timeout-minutes: 40', workflow)
        self.assertIn("id: google_provider", workflow)
        self.assertIn("steps.google_provider.outputs.provider != 'hermes-local'", workflow)
        self.assertIn("if: always() && steps.google.outcome != 'skipped'", workflow)
        self.assertIn('python audit_google_comparisons.py --root . --state google-places-state.json --status google-places-status.json --limit 45', workflow)
        self.assertIn('GOOGLE_PLACES_ENABLED: ${{ vars.GOOGLE_PLACES_ENABLED }}', workflow)
        self.assertIn('GOOGLE_PLACES_API_KEY: ${{ secrets.GOOGLE_PLACES_API_KEY }}', workflow)
        self.assertIn('files=(google-places-status.json)', workflow)
        self.assertIn('if [ -f google-places-state.json ]; then files+=(google-places-state.json); fi', workflow)
        self.assertIn('git add -- "${files[@]}"', workflow)
        artifacts = workflow.split('      - name: Upload Google comparison artifacts', 1)[1]
        self.assertIn('google-places-state.json', artifacts)
        self.assertIn('google-places-status.json', artifacts)
        self.assertIn("if: always() && steps.google.outcome != 'skipped'", artifacts)
        self.assertIn('retention-days: 7', artifacts)
        self.assertNotIn('google-places-state.json', workflow.split('      - name: Upload health report and candidate artifacts', 1)[1].split('      - name:', 1)[0])
        notifications = (ROOT / '.github/workflows/location-alerts.yml').read_text()
        self.assertIn("load_google_status('google-places-status.json')", notifications)
        self.assertIn('report.google_count || report.health_count || failed', notifications)
        self.assertIn('expected_google_run_id', notifications)
        self.assertIn('str(status.get(\'run_id\')) != expected', notifications)
        self.assertIn('hermes-google-status.json', notifications.split('paths:', 1)[1].split('\n', 1)[0])
        self.assertIn('GITHUB_STEP_SUMMARY', notifications)
        self.assertIn('ALERT_TO: ${{ secrets.ALERT_TO }}', notifications)

    @unittest.skipUnless(GIT and BASH, 'Git and Bash execute the actual persistence step offline')
    def test_persistence_step_commits_status_even_when_failure_precedes_state_creation(self):
        workflow = (ROOT / '.github/workflows/weekly-map-validation.yml').read_text()
        script = textwrap.dedent(workflow.split('      - name: Persist Google comparison status', 1)[1]
                                 .split('        run: |', 1)[1].split('      - name:', 1)[0])
        for state_exists in (False, True):
            with self.subTest(state_exists=state_exists), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory) / 'checkout'
                remote = Path(directory) / 'local-remote.git'
                repo.mkdir()
                # The push target is a temporary local bare repository; no network.
                def git(*args, cwd=repo):
                    return subprocess.run([GIT, *args], cwd=cwd, text=True, capture_output=True, check=True)
                git('init', '--quiet')
                git('init', '--bare', '--quiet', str(remote))
                git('remote', 'add', 'origin', str(remote))
                git('config', 'remote.origin.push', 'HEAD:refs/heads/main')
                (repo / 'google-places-status.json').write_text(json.dumps(self.status(hard_failure=True)))
                if state_exists:
                    (repo / 'google-places-state.json').write_text('{"schema_version": 1}')
                subprocess.run([BASH, '-e', '-c', script], cwd=repo, text=True, capture_output=True, check=True)
                files = git('ls-tree', '--name-only', 'HEAD').stdout.splitlines()
                self.assertIn('google-places-status.json', files)
                self.assertEqual('google-places-state.json' in files, state_exists)

    @unittest.skipUnless(shutil.which('node'), 'Node executes the actual GitHub notification script')
    def test_google_only_issues_open_review_and_saved_failures_do_not_force_repeat_alerts(self):
        workflow = (ROOT / '.github/workflows/location-alerts.yml').read_text()
        script = textwrap.dedent(workflow.split('      - name: Update the persistent GitHub review issue', 1)[1]
                                 .split('          script: |', 1)[1].split('      - name:', 1)[0])
        receipt = dict(google_count=1, google_hard_failure=True, google_run_id='10', google_run_attempt=1,
                       health_run_id='10', health_run_attempt=1)
        google_step = dict(name='Rotate Google Places comparisons', status='completed', conclusion='failure')
        def evidence(steps=None, **changes):
            return dict(dict(total_count=1, jobs=[dict(status='completed', conclusion='failure',
                         steps=steps if steps is not None else [google_step])]), **changes)
        cases = [
            # Google-only findings keep the persistent issue open.
            ({'google_count': 1}, {'conclusion': 'success'}, None, '', 'open'),
            # Matching receipts plus execution-linked Google-only failure evidence deduplicate.
            (receipt, {}, evidence(), '', 'open'),
            (receipt, {}, evidence([dict(google_step, conclusion='timed_out')]), '', 'open'),
            # Additional failed upload or persistence steps remain pipeline failures.
            (receipt, {}, evidence([google_step, dict(name='Upload health report and candidate artifacts',
              status='completed', conclusion='failure')]), 'https://example.test/run', 'open'),
            (receipt, {}, evidence([google_step, dict(name='Persist Google comparison status and rotation, including partial failures',
              status='completed', conclusion='failure')]), 'https://example.test/run', 'open'),
            # Missing, unavailable, incomplete or unknown evidence is inconclusive.
            (receipt, {}, None, 'https://example.test/run', 'open'),
            (receipt, {}, {}, 'https://example.test/run', 'open'),
            (receipt, {}, evidence([]), 'https://example.test/run', 'open'),
            (receipt, {}, evidence(total_count=2), 'https://example.test/run', 'open'),
            (receipt, {}, evidence(jobs=[dict(status='completed', conclusion='failure')]), 'https://example.test/run', 'open'),
            (receipt, {}, evidence([dict(google_step, conclusion=None)]), 'https://example.test/run', 'open'),
            (receipt, {}, evidence([dict(google_step, conclusion='success')]), 'https://example.test/run', 'open'),
            (receipt, {}, evidence([dict(google_step, name='Unknown failed step')]), 'https://example.test/run', 'open'),
            # A configuration limitation does not disguise unrelated workflow failures.
            ({'google_count': 1, 'google_hard_failure': False, 'google_run_id': '10', 'google_run_attempt': 1,
              'health_run_id': '10', 'health_run_attempt': 1}, {}, None, 'https://example.test/run', 'open'),
            # A prior attempt or missing health receipt cannot hide a new pipeline failure.
            ({'google_count': 1, 'google_run_id': '10', 'google_run_attempt': 1,
              'health_run_id': '10', 'health_run_attempt': 1}, {'run_attempt': 2}, evidence(), 'https://example.test/run', 'open'),
            ({'google_count': 1, 'google_run_id': '10'}, {}, evidence(), 'https://example.test/run', 'open'),
            ({'google_count': 0, 'health_run_id': '10'}, {'conclusion': 'success'}, None, '', 'closed'),
        ]
        harness = '''
        const {script, cases} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
        (async () => {
          const outputs = [];
          for (const [snapshot, event, evidence] of cases) {
            const updates = [], exported = {};
            const report = {...snapshot, active_count: 0};
            const fs = {readFileSync: path => path.endsWith('.json') ? JSON.stringify(report) : 'Current report'};
            const github = {rest: {issues: {
              get: async () => ({data: {number: 5, state: 'open', body: 'Old'}}),
              update: async args => updates.push(args)
            }, actions: {listJobsForWorkflowRunAttempt: async args => {
              if (!evidence) throw new Error('Job evidence unavailable');
              if (args.run_id !== 10 || args.attempt_number !== 1 || args.per_page !== 100)
                throw new Error('Wrong execution requested');
              return {data: evidence};
            }}}};
            const context = {repo: {owner: 'test', repo: 'test'}, payload: {workflow_run: {
              id: 10, run_attempt: 1, name: 'Daily map validation', conclusion: 'failure',
              html_url: 'https://example.test/run', ...event
            }}};
            await new AsyncFunction('require', 'github', 'context', 'core', script)(
              () => fs, github, context, {exportVariable: (key, value) => exported[key] = value});
            outputs.push([exported.FAILURE_URL, updates[0].state]);
          }
          process.stdout.write(JSON.stringify(outputs));
        })().catch(error => { console.error(error); process.exit(1); });
        '''
        result = subprocess.run([shutil.which('node'), '-e', harness], input=json.dumps(
            dict(script=script, cases=[[snapshot, event, evidence] for snapshot, event, evidence, _, _ in cases])),
            text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [[url, state] for _, _, _, url, state in cases])
