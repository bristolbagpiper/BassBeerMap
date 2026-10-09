import copy
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from full_location_audit import assess, compare, select_rows, run
from location_review import build_review
from directory_release import build_ledger, write_release, read_json, validate_release
from tests.test_directory_release import row, registry_for, pin


def business(name='Example Inn', lat=51.45, lng=-2.59, postcode='BS1 1AA'):
    return dict(FHRSID=123, BusinessName=name, PostCode=postcode, AddressLine1='1 Example Road',
                geocode=dict(latitude=str(lat), longitude=str(lng)))


class IndependentAuditTests(unittest.TestCase):
    def test_disagreement_does_not_move_owner_approved_pin(self):
        listing = row()
        registry = registry_for([listing])
        entry = registry['venues'][listing['venue_id']]
        entry['location_approvals'] = [{'owner_instruction': 'correct'}]
        before = copy.deepcopy(entry)
        check = assess(listing, entry, lookup=lambda *a, **k: [business(lat=51.46)])
        self.assertEqual(check['status'], 'coordinate_disagreement')
        self.assertEqual(check['confidence'], 'owner_reviewed')
        self.assertEqual(before, entry)
        entry['independent_location_check'] = check
        review = build_review([listing], registry, build_ledger([listing], registry))
        self.assertEqual(review['unverified_count'], 0)
        self.assertEqual(review['active_count'], 0)
        self.assertFalse(check['actionable'])
        self.assertTrue(check['warnings'])
        self.assertEqual(entry['verified_pin'], before['verified_pin'])

    def test_no_match_outage_and_ambiguous_points_are_inconclusive(self):
        self.assertEqual(compare(row(), pin(), [])['status'], 'no_name_match')
        self.assertEqual(compare(row(), pin(), [business(), business(lat=51.46)])['status'], 'ambiguous_or_missing_coordinates')
        listing = row()
        registry = registry_for([listing])
        def failed(*a, **k):
            raise TimeoutError()
        check = assess(listing, registry['venues'][listing['venue_id']], failed)
        self.assertEqual(check['status'], 'unavailable')
        self.assertNotEqual(check['confidence'], 'independent_coordinate_agreement')

    def test_same_source_never_counts_as_independent_corroboration(self):
        listing = row()
        entry = dict(verified_pin=pin())
        entry['verified_pin']['evidence'].append(dict(source='food_standards_agency', url='https://ratings.food.gov.uk/business/123'))
        check = assess(listing, entry, lambda *a, **k: [business()])
        self.assertEqual(check['status'], 'coordinate_agreement')
        self.assertFalse(check['independent_of_pin'])
        self.assertNotEqual(check['confidence'], 'independent_coordinate_agreement')

    def test_stale_comparison_does_not_flag_newly_reviewed_point(self):
        listing = row()
        registry = registry_for([listing])
        entry = registry['venues'][listing['venue_id']]
        entry['independent_location_check'] = dict(pin={'lat': 51.46, 'lng': -2.59}, status='coordinate_disagreement', reasons=['old discrepancy'])
        self.assertEqual(build_review([listing], registry, build_ledger([listing], registry))['active_count'], 0)

    def test_rotation_uses_oldest_checks_before_repeat_conflicts(self):
        rows = [row('One'), row('Two'), row('Three')]
        registry = registry_for(rows)
        registry['venues'][rows[0]['venue_id']]['verified_pin']['metadata_issue'] = {'reference': 'BS1 1AB'}
        checks = {rows[0]['venue_id']: {'checked_at': '2026-10-09', 'pin': {'lat':51.45,'lng':-2.59}, 'listing': {'pub_name':'One','postcode':'BS1 1AA'}}}
        selected = select_rows(rows, registry, checks, 2)
        self.assertNotIn(rows[0], selected)

    def test_reference_postcode_checked_without_erasing_listed_conflict(self):
        listing = row()
        entry = dict(verified_pin=pin())
        entry['verified_pin']['metadata_issue'] = dict(reference='BS1 1AB', listed='BS1 1AA')
        queries = []
        def lookup(query, **kwargs):
            queries.append(query['postcode'])
            return [business(postcode=query['postcode'])]
        check = assess(listing, entry, lookup)
        self.assertEqual(queries, ['BS1 1AA', 'BS1 1AB'])
        self.assertEqual(check['priority'], 0)
        self.assertTrue(check['reasons'])

    def test_publish_keeps_live_coordinates_and_exposes_every_location(self):
        rows = [row('Example Inn'), row('Another Inn')]
        registry = registry_for(rows)
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory)/'root', Path(directory)/'output'
            write_release(root, rows, registry, {}, {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}})
            (root/'latest-bass-directory.pdf').write_bytes(b'%PDF-1.4 test fixture')
            before = read_json(root/'directory-release.json')
            def mocked_assess(listing, entry):
                return assess(listing, entry, lambda *a, **k: [business(name=listing['pub_name'], lat=51.46)])
            with patch('full_location_audit.assess', side_effect=mocked_assess):
                run(root, output, publish=True)
            self.assertEqual(read_json(root/'directory-release.json'), before)
            self.assertEqual(len(read_json(root/'audit/full-location-audit.json')['records']), 2)
            self.assertEqual(read_json(root/'location-review.json')['active_count'], 0)
            self.assertEqual(read_json(root/'location-review.json')['unverified_count'], 0)
            self.assertEqual(validate_release(root), [])

    def test_identity_change_invalidates_old_comparison(self):
        listing = row()
        registry = registry_for([listing])
        entry = registry['venues'][listing['venue_id']]
        entry['independent_location_check'] = assess(listing, entry, lambda *a, **k: [business(lat=51.46)])
        entry['independent_location_check']['listing']['pub_name'] = 'Old Name'
        self.assertEqual(build_review([listing], registry, build_ledger([listing], registry))['active_count'], 0)

    def test_missing_pin_is_not_misreported_as_source_outage(self):
        with patch('full_location_audit.search') as lookup:
            check = assess(row(), {'verified_pin': None}, lookup)
        lookup.assert_not_called()
        self.assertEqual(check['status'], 'no_accepted_pin')
        self.assertEqual(check['priority'], 0)

    def test_reclassification_resolves_coordinate_only_alert_and_keeps_history(self):
        listing = row()
        registry = registry_for([listing])
        identifier = listing['venue_id']
        entry = registry['venues'][identifier]
        entry['independent_location_check'] = assess(listing, entry, lambda *a, **k: [business(lat=51.46)])
        registry['location_review'] = {'records': {identifier: dict(active=True, history=[dict(date='2026-10-08', status='review_needed', reasons=['FSA coordinate differs'])])}}
        review = build_review([listing], registry, build_ledger([listing], registry))
        self.assertEqual(review['active_count'], 0)
        self.assertEqual(len(review['records'][identifier]['history']), 2)
        self.assertEqual(review['records'][identifier]['status'], 'resolved')

    def test_postcode_conflict_remains_actionable_despite_coordinate_agreement(self):
        listing = row()
        registry = registry_for([listing])
        entry = registry['venues'][listing['venue_id']]
        entry['verified_pin']['metadata_issue'] = dict(listed='BS1 1AA', reference='BS1 1AB')
        check = assess(listing, entry, lambda query, **k: [business(postcode=query['postcode'])])
        self.assertTrue(check['actionable'])
        entry['independent_location_check'] = check
        self.assertEqual(build_review([listing], registry, build_ledger([listing], registry))['active_count'], 1)

    def test_reports_only_never_queries_sources_or_refreshes_dates(self):
        rows = [row()]
        registry = registry_for(rows)
        check = assess(rows[0], registry['venues'][rows[0]['venue_id']], lambda *a, **k: [business(lat=51.46)])
        check['checked_at'] = '2026-10-08'
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory)/'root', Path(directory)/'output'
            write_release(root, rows, registry, {}, {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}})
            from directory_release import write_json
            write_json(root/'independent-audit-state.json', {'checks': {rows[0]['venue_id']: check}})
            with patch('full_location_audit.assess') as lookup:
                run(root, output, reports_only=True)
            lookup.assert_not_called()
            saved = read_json(output/'independent-audit-state.json')['checks'][rows[0]['venue_id']]
            self.assertEqual(saved['checked_at'], '2026-10-08')
            self.assertFalse(saved['actionable'])
            self.assertTrue(saved['warnings'])
