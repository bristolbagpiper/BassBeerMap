import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowStructureTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / '.github' / 'workflows' / name).read_text(encoding='utf-8')

    def test_recurring_external_research_is_manual_only(self):
        independent = self.read('independent-location-audit.yml')
        research = self.read('research-locations.yml')
        self.assertIn('  workflow_dispatch:', independent)
        self.assertNotIn('  schedule:', independent)
        self.assertIn('  workflow_dispatch:', research)
        self.assertNotIn('  schedule:', research)
        self.assertNotIn('  push:', research)
        self.assertIn('contents: read', research)
        self.assertIn('validate_directory_release.py --root candidate-research --previous .', research)
        self.assertIn('actions/upload-artifact@v4', research)
        self.assertNotIn('git add', research)
        self.assertNotIn('git push', research)
        self.assertNotIn('copyfile', research)

    def test_scheduled_map_integrity_check_is_read_only_and_independent_of_local_google(self):
        source = self.read('weekly-map-validation.yml')
        self.assertIn('  schedule:', source)
        self.assertNotIn('  workflow_run:', source)
        self.assertIn('python audit_map_health.py --integrity-only', source)
        self.assertIn('contents: read', source)
        self.assertNotIn('map-health-status.json', source)
        self.assertNotIn('validate_google_handoff.py', source)
        self.assertNotIn('audit_google_comparisons.py', source)
        self.assertNotIn('git add', source)
        self.assertNotIn('git push', source)

    def test_alert_routes_preserve_notice_and_approved_correction_updates(self):
        source = self.read('location-alerts.yml')
        self.assertIn("workflows: ['Apply reviewed location']", source)
        update = (ROOT / '.github/workflows/update-csv-from-pdf.yml').read_text(encoding='utf-8')
        self.assertIn("needs.update-csv.outputs.published_on != ''", update)
        self.assertIn("steps.pages_release.outcome == 'success'", update)
        self.assertIn("workflow_id: 'location-alerts.yml'", update)
        self.assertIn('  workflow_dispatch:', source)
        paths = source.split('    paths:', 1)[1].split('\n\npermissions:', 1)[0]
        for path in ('trading-notices.json', 'directory-release.json'):
            self.assertIn(path, paths)
        self.assertNotIn('expected_google_run_id', source)
        self.assertNotIn('validate_google_handoff.py', source)
        self.assertNotIn('map-health-status.json', source)
        self.assertNotIn('google-places-status.json', source)
        self.assertIn('validate_directory_release.py --allow-expired-evidence', source)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", source)
        self.assertIn('  contents: read', source)
        self.assertIn('  issues: write', source)
        self.assertNotIn('python apply_closed_reviews.py', source)
        self.assertNotIn('contents: write', source)
        self.assertNotIn('git add location-alert-state.json', source)
        self.assertNotIn('promote_checked_commit.py', source)
        self.assertIn('uncertain_resolution:', source)
        self.assertIn('confirm_sent', source)
        self.assertIn('confirm_not_sent', source)
        self.assertIn('--issue', Path(__file__).resolve().parents[1].joinpath('notify_location_review.py').read_text(encoding='utf-8'))
        self.assertNotIn('location-alert-state.json', source.split('name: Upload review report', 1)[1])

    def test_rejected_import_has_its_own_durable_issue(self):
        source = self.read('update-csv-from-pdf.yml')
        self.assertIn("const title = 'BassBeerMap update rejected'", source)
        self.assertIn('The previous published release is retained.', source)
        self.assertIn('name: Report rejected import', source)
        self.assertIn("if: failure() && steps.canonical_event.outputs.current != 'false'", source)

    def test_normal_release_publish_paths_are_import_and_reviewed_correction_only(self):
        import_workflow = self.read('update-csv-from-pdf.yml')
        correction_workflow = self.read('approve-location.yml')
        map_check = self.read('weekly-map-validation.yml')
        research = self.read('research-locations.yml')
        self.assertIn('Publish the complete validated release', import_workflow)
        self.assertIn('Publish the validated review', correction_workflow)
        self.assertIn('contents: read', map_check)
        self.assertNotIn('git push', map_check)
        self.assertNotIn('git push', research)


if __name__ == '__main__':
    unittest.main()
