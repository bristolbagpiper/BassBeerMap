import csv
import tempfile
import unittest
from pathlib import Path

from full_location_audit import assess, report
from tests.test_directory_release import row, registry_for, pin
from tests.test_full_location_audit import business


class AuditReportCategoryTests(unittest.TestCase):
    def test_large_fsa_disagreement_is_information_not_pin_task(self):
        listing = row()
        entry = registry_for([listing])['venues'][listing['venue_id']]
        check = assess(listing, entry, lambda *a, **k: [business(lat=51.46)])
        self.assertEqual(check['status'], 'coordinate_disagreement')
        self.assertFalse(check['manual_action_required'])
        self.assertEqual(check['review_category'], 'informational')
        self.assertEqual(check['next_action'], 'No pin check requested.')

    def test_missing_fsa_name_and_source_outage_are_not_pin_tasks(self):
        listing = row()
        entry = registry_for([listing])['venues'][listing['venue_id']]
        missing = assess(listing, entry, lambda *a, **k: [])
        self.assertFalse(missing['manual_action_required'])
        self.assertIn('absence of a match', missing['warnings'][0].lower())
        unavailable = assess(listing, entry, lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(unavailable['review_category'], 'source_warning')
        self.assertFalse(unavailable['manual_action_required'])
        self.assertFalse(unavailable['reasons'])
        self.assertIn('unavailable', unavailable['warnings'][0].lower())

    def test_absent_pin_evidence_requests_verification_without_claiming_wrong_pin(self):
        listing = row()
        check = assess(listing, {'verified_pin': None})
        self.assertEqual(check['review_category'], 'needs_verification')
        self.assertTrue(check['manual_action_required'])
        self.assertIn('Verify', check['next_action'])
        self.assertNotIn('wrong', check['next_action'].lower())

    def test_postcode_discrepancy_is_address_metadata_action_and_reports_all_records(self):
        rows = [row('One Inn'), row('Two Inn')]
        registry = registry_for(rows)
        entry = registry['venues'][rows[0]['venue_id']]
        entry['verified_pin']['metadata_issue'] = {'listed': 'BS1 1AA', 'reference': 'BS1 1AB'}
        check = assess(rows[0], entry, lambda query, **k: [business(postcode=query['postcode'])])
        self.assertEqual(check['review_category'], 'address_metadata')
        self.assertTrue(check['manual_action_required'])
        checks = {rows[0]['venue_id']: check}
        with tempfile.TemporaryDirectory() as directory:
            summary = report(rows, registry, checks, Path(directory))
            self.assertEqual(summary['total'], 2)
            self.assertEqual(summary['action_required_count'], 1)
            self.assertEqual(summary['information_no_pin_check_count'], 1)
            import json
            records = json.loads((Path(directory) / 'full-location-audit.json').read_text())['records']
            self.assertEqual(len(records), 2)
            with (Path(directory) / 'full-location-audit.csv').open(encoding='utf-8', newline='') as stream:
                csv_rows = list(csv.DictReader(stream))
            self.assertEqual(len(csv_rows), 2)
            self.assertIn('review_category', csv_rows[0])
            self.assertIn('manual_action_required', csv_rows[0])
            markdown = (Path(directory) / 'full-location-audit.md').read_text()
            action_section = markdown.split('## Action required', 1)[1].split('## Information', 1)[0]
            self.assertIn('One Inn', action_section)
            self.assertNotIn('Two Inn', action_section)
            self.assertIn('Information', markdown)
            self.assertIn('no pin check requested', markdown)


if __name__ == '__main__':
    unittest.main()
