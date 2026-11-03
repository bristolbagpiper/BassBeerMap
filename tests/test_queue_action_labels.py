import copy
import unittest

from build_place_review_queue import build_queue
from owner_location_review import listing_snapshot
from tests.test_place_review_queue import fixture


class QueueActionLabelTests(unittest.TestCase):
    def test_fsa_disagreement_and_inconclusive_states_are_information_only(self):
        for status, comparisons in (
            ('coordinate_disagreement', [dict(difference_metres=400)]),
            ('nearby', [dict(difference_metres=30)]),
            ('no_name_match', []),
            ('unavailable', [dict(status='unavailable')]),
        ):
            bundle, registry, report = fixture()
            report['records'][0].update(status=status, comparisons=comparisons,
                                        reasons=['FSA comparison unavailable'] if status == 'unavailable' else [])
            item = build_queue(bundle, registry, report)[0]
            with self.subTest(status=status):
                self.assertFalse(item['manual_action_required'])
                self.assertEqual(item['next_action'], 'No pin check requested')
                self.assertEqual(item['google_check_status'], 'pending')

    def test_approximate_or_explicitly_quarantined_pin_is_actionable(self):
        bundle, registry, report = fixture()
        bundle['locations']['a']['precision'] = 'postcode'
        item = build_queue(bundle, registry, report)[0]
        self.assertTrue(item['manual_action_required'])
        self.assertEqual(item['review_category'], 'needs_verification')
        bundle, registry, report = fixture()
        registry['venues']['a']['verified_pin']['review_required'] = True
        self.assertTrue(build_queue(bundle, registry, report)[0]['manual_action_required'])

    def test_actionable_current_review_and_source_warning_are_distinct(self):
        bundle, registry, report = fixture()
        registry['location_review'] = {'records': {'a': dict(active=True, reasons=['Venue name changed'],
                                                            review_category='identity_check', manual_action_required=True,
                                                            next_action='Check identity')}}
        action = build_queue(bundle, registry, report)[0]
        self.assertTrue(action['manual_action_required'])
        self.assertEqual(action['review_category'], 'identity_check')
        bundle, registry, report = fixture()
        registry['location_review'] = {'records': {'a': dict(active=True, reasons=['Directory postcode BS1 conflicts with reference address BS2'],
                                                            review_category='address_metadata', manual_action_required=True,
                                                            next_action='Check the conflicting address/postcode')}}
        conflict = build_queue(bundle, registry, report)[0]
        self.assertTrue(conflict['manual_action_required'])
        self.assertEqual(conflict['review_category'], 'address_metadata')
        bundle, registry, report = fixture()
        registry['location_review'] = {'records': {'a': dict(active=True, reasons=['Verification source unavailable: timeout'])}}
        warning = build_queue(bundle, registry, report)[0]
        self.assertFalse(warning['manual_action_required'])
        self.assertEqual(warning['review_category'], 'source_warning')
        self.assertIn('retry the source', warning['next_action'])

    def test_current_owner_confirmation_is_exactly_point_bound_and_history_is_informational(self):
        bundle, registry, report = fixture()
        row = bundle['rows'][0]
        pin = registry['venues']['a']['verified_pin']
        pin['owner_confirmation'] = dict(schema_version=1, scope='displayed_venue_building',
                                         listing=listing_snapshot(row), approved_coordinate={'lat': pin['lat'], 'lng': pin['lng']},
                                         checked_at='2026-10-31', reviewed_by='owner', owner_instruction='confirmed point')
        registry['venues']['a']['location_approvals'] = [{'checked_at': '2026-10-31'}]
        item = build_queue(bundle, registry, report)[0]
        self.assertTrue(item['owner_pin_confirmed'])
        self.assertFalse(item['manual_action_required'])
        self.assertEqual(item['next_action'], 'No pin check requested')
        changed = copy.deepcopy(bundle)
        changed['locations']['a']['lat'] += 0.001
        registry_changed = copy.deepcopy(registry)
        registry_changed['venues']['a']['verified_pin']['lat'] += 0.001
        changed_item = build_queue(changed, registry_changed, report)[0]
        self.assertFalse(changed_item['owner_pin_confirmed'])
        self.assertFalse(changed_item['manual_action_required'])


if __name__ == '__main__':
    unittest.main()
