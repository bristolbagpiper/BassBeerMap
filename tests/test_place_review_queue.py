import copy
import unittest
from datetime import date
from urllib.parse import parse_qs, urlparse

from build_place_review_queue import apply_receipts, build_queue, make_receipt, non_google, receipt_is_current


def fixture():
    rows = [dict(venue_id='a', pub_name='Example Inn', place_name='Bristol', postcode='BS1 1AA',
                 area='Somerset', country='England')]
    location = dict(lat=51.45, lng=-2.59, precision='venue')
    pin = dict(lat=51.45, lng=-2.59, source='manual', evidence=[dict(
        source='camra', url='https://camra.org.uk/pubs/example', reference_address='1 Example Road',
        reference_postcode='BS1 1AA')])
    bundle = dict(rows=rows, locations={'a': location})
    registry = dict(venues={'a': dict(verified_pin=pin)})
    report = dict(records=[dict(venue_id='a', listing=dict(pub_name='Example Inn', postcode='BS1 1AA'),
                               pin=dict(lat=51.45, lng=-2.59), confidence='independent_coordinate_agreement',
                               status='coordinate_agreement', checked_at='2026-10-31', comparisons=[])])
    return bundle, registry, report


class PlaceReviewQueueTests(unittest.TestCase):
    def test_links_are_named_and_own_sources_never_imply_google_check(self):
        bundle, registry, report = fixture()
        registry['venues']['a']['verified_pin']['evidence'].append(dict(
            source='google-maps-named-place', url='https://www.google.com/maps/place/old',
            reference_address='Google copied address'))
        original = copy.deepcopy((bundle, registry, report))
        item = build_queue(bundle, registry, report)[0]
        query = parse_qs(urlparse(item['google_named_search_url']).query)['query'][0]
        self.assertIn('Example Inn', query)
        self.assertIn('1 Example Road', query)
        self.assertNotIn('Google copied address', query)
        self.assertNotIn('Google copied address', str(item))
        self.assertEqual(item['google_check_status'], 'pending')
        self.assertFalse(item['google_verified'])
        self.assertEqual(original, (bundle, registry, report))

    def test_coverage_excludes_registry_retired_and_rejects_duplicate_active_ids(self):
        bundle, registry, report = fixture()
        registry['venues']['retired'] = copy.deepcopy(registry['venues']['a'])
        self.assertEqual([q['venue_id'] for q in build_queue(bundle, registry, report)], ['a'])
        bundle['rows'].append(copy.deepcopy(bundle['rows'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate active'):
            build_queue(bundle, registry, report)

    def test_stale_fsa_is_not_used_as_current_corroboration_or_search_address(self):
        bundle, registry, report = fixture()
        report['records'][0]['pin']['lat'] = 51.5
        report['records'][0]['comparisons'] = [dict(address='Old FSA address', difference_metres=9000,
                                                 url='https://ratings.food.gov.uk/business/1')]
        item = build_queue(bundle, registry, report)[0]
        self.assertEqual(item['fsa_status'], 'stale_or_missing')
        self.assertEqual(item['confidence'], 'not_checked')
        self.assertIsNone(item['largest_fsa_difference_metres'])
        self.assertNotIn('Old FSA address', str(item))

    def test_risk_order_and_displayed_fallback_point(self):
        bundle, registry, report = fixture()
        base = copy.deepcopy(bundle['rows'][0])
        for identifier in ('quarantine', 'acknowledged', 'large', 'uncorroborated', 'owner'):
            bundle['rows'].append(dict(base, venue_id=identifier))
            bundle['locations'][identifier] = copy.deepcopy(bundle['locations']['a'])
            registry['venues'][identifier] = copy.deepcopy(registry['venues']['a'])
            report['records'].append(dict(copy.deepcopy(report['records'][0]), venue_id=identifier))
        bundle['locations']['quarantine'] = dict(lat=51.46, lng=-2.58, precision='postcode')
        registry['venues']['acknowledged']['acknowledged_trading_review'] = {'checked_at': '2026-10-31'}
        report['records'][3]['comparisons'] = [dict(difference_metres=800)]
        report['records'][4]['confidence'] = 'existing_evidence_needs_corroboration'
        registry['venues']['owner']['location_approvals'] = [{'checked_at': '2026-10-31'}]
        queue = build_queue(bundle, registry, report)
        self.assertEqual([q['venue_id'] for q in queue], ['quarantine', 'acknowledged', 'large', 'uncorroborated', 'owner', 'a'])
        self.assertEqual(queue[0]['current_pin']['lat'], 51.46)
        self.assertEqual(queue[0]['current_pin_source'], 'postcode_fallback')

    def test_receipts_are_exactly_bound_and_never_authorize_pin_promotion(self):
        bundle, registry, report = fixture()
        item = build_queue(bundle, registry, report)[0]
        receipt = make_receipt(item, 'named_place_identity_and_pin_consistent', '2026-10-31',
                               'reviewer', 'https://www.google.com/maps/place/example')
        self.assertTrue(receipt_is_current(receipt, item))
        self.assertFalse(receipt['pin_promotion_authorized'])
        for field, value in [('pub_name', 'Renamed Inn'), ('place_name', 'Other Bristol'), ('postcode', 'BS1 1AB')]:
            changed = copy.deepcopy(bundle)
            changed['rows'][0][field] = value
            self.assertFalse(receipt_is_current(receipt, build_queue(changed, registry, report)[0]))
        for field, value in [('lat', 51.46), ('lng', -2.58), ('precision', 'postcode')]:
            changed = copy.deepcopy(bundle)
            changed['locations']['a'][field] = value
            self.assertFalse(receipt_is_current(receipt, build_queue(changed, registry, report)[0]))
        with self.assertRaises(ValueError):
            make_receipt(item, 'google_verified', '2026-10-31', 'reviewer', 'https://www.google.com/maps/place/x')
        with self.assertRaises(ValueError):
            make_receipt(item, 'ambiguous_results', '2027-01-01', 'reviewer',
                         'https://www.google.com/maps/place/x', today=date(2026, 11, 2))
        with self.assertRaises(ValueError):
            make_receipt(item, 'ambiguous_results', '2026-10-31', 'reviewer', 'https://evil.example/google.com')

    def test_identity_only_is_distinct_from_pin_observation(self):
        item = build_queue(*fixture())[0]
        url = 'https://www.google.com/maps/place/Example+Inn/@51.45,-2.59,17z/'
        receipt = make_receipt(item, 'named_place_identity_consistent', '2026-10-31', 'reviewer', url)
        self.assertTrue(receipt['identity_observed'])
        self.assertFalse(receipt['pin_observed'])
        self.assertTrue(receipt_is_current(receipt, item))
        receipt['pin_observed'] = True
        self.assertFalse(receipt_is_current(receipt, item))

    def test_named_categories_require_named_place_urls(self):
        item = build_queue(*fixture())[0]
        invalid = [
            'https://www.google.com/',
            item['current_pin_url'], item['google_named_search_url'],
            '51.45,-2.59',
            'https://www.google.com/maps/place/51.45,-2.59',
            'https://www.google.com/maps/place/@51.45,-2.59',
            'https://www.google.com/maps/place/Example/../../search/',
            'https://www.google.com/maps/place/%2E%2E/search/',
            'https://www.google.com/maps/place/Example%2F%2Fsearch',
            'https://www.google.com/maps/place/Example%252Fsearch',
            'https://www.google.com.evil.example/maps/place/Example',
            'https://www.google.com@evil.example/maps/place/Example',
            'https://www.google.com/other/maps/place/Example',
        ]
        for category in ('named_place_identity_consistent', 'named_place_identity_and_pin_consistent',
                         'named_place_identity_conflict', 'named_place_pin_conflict', 'trading_status_conflict'):
            for url in invalid:
                with self.subTest(category=category, url=url), self.assertRaises(ValueError):
                    make_receipt(item, category, '2026-10-31', 'reviewer', url)
            self.assertTrue(make_receipt(item, category, '2026-10-31', 'reviewer',
                                        'https://www.google.com/maps/place/Example+Inn'))
        for category in ('unavailable', 'ambiguous_results', 'no_named_place'):
            self.assertTrue(make_receipt(item, category, '2026-10-31', 'reviewer', item['google_named_search_url']))

    def test_reference_addresses_require_exact_source_host_mapping(self):
        for source, host in [('camra', 'camra.org.uk'), ('fsa', 'ratings.food.gov.uk'),
                             ('fhrs', 'api.ratings.food.gov.uk'), ('openstreetmap', 'www.openstreetmap.org')]:
            self.assertTrue(non_google(dict(source=source, url='https://' + host + '/reference')))
        for source, host in [('camra', 'maps.google.co.uk'), ('camra', 'camra.org.uk.evil.example'),
                             ('camra', 'evil.example'), ('camra', 'ratings.food.gov.uk'),
                             ('operator', 'unreviewed.example')]:
            with self.subTest(source=source, host=host):
                self.assertFalse(non_google(dict(source=source, url='https://' + host + '/reference')))
                bundle, registry, report = fixture()
                registry['venues']['a']['verified_pin']['evidence'] = [dict(
                    source=source, url='https://' + host + '/reference', reference_address='Do not copy')]
                self.assertNotIn('Do not copy', str(build_queue(bundle, registry, report)))

    def test_apply_receipts_multiple_categories_and_stale_binding(self):
        bundle, registry, report = fixture()
        original = build_queue(bundle, registry, report)
        receipt = make_receipt(original[0], 'named_place_identity_consistent', '2026-10-31',
                               'reviewer', 'https://www.google.com/maps/place/Example+Inn')
        second = make_receipt(original[0], 'ambiguous_results', '2026-10-31',
                              'reviewer', original[0]['google_named_search_url'])
        queue, summary = apply_receipts(original, [receipt, second])
        self.assertEqual(summary['accepted_receipt_count'], 2)
        self.assertEqual(summary['observed_venue_count'], 1)
        self.assertEqual(queue[0]['google_check_status'], 'observed')
        self.assertEqual(len(queue[0]['google_checks']), 2)
        self.assertFalse(queue[0]['google_verified'])
        self.assertEqual(original[0]['google_check_status'], 'pending')
        for change in ('name', 'pin'):
            changed = copy.deepcopy(bundle)
            if change == 'name':
                changed['rows'][0]['pub_name'] = 'Renamed'
            else:
                changed['locations']['a']['lat'] = 51.46
            queue, summary = apply_receipts(build_queue(changed, registry, report), [receipt])
            self.assertEqual(summary['observed_venue_count'], 0)
            self.assertEqual(summary['discarded_receipt_count'], 1)
            self.assertEqual(queue[0]['google_check_status'], 'pending')
            self.assertEqual(queue[0]['receipt_rejections'][0]['reason'], 'identity_or_point_signature_changed')

    def test_apply_receipts_unknown_duplicate_and_malformed(self):
        queue = build_queue(*fixture())
        receipt = make_receipt(queue[0], 'named_place_identity_consistent', '2026-10-31',
                               'reviewer', 'https://www.google.com/maps/place/Example+Inn')
        unknown = dict(receipt, venue_id='retired')
        result, summary = apply_receipts(queue, [unknown])
        self.assertEqual(summary['rejected_reason_counts'], {'unknown_active_venue_id': 1})
        self.assertEqual(summary['pending_venue_count'], 1)
        self.assertEqual(result[0]['google_check_status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'Duplicate receipt'):
            apply_receipts(queue, [receipt, receipt])
        for malformed in (None, {}, [None], [{}], [dict(receipt, pin_promotion_authorized=True)],
                          [dict(receipt, identity_signature='invalid')], [dict(receipt, extra='data')],
                          [dict(receipt, result_url=queue[0]['current_pin_url'])]):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                apply_receipts(queue, malformed)

    def test_receipt_conflicts_sort_first_without_changing_points(self):
        bundle, registry, report = fixture()
        bundle['rows'].append(dict(bundle['rows'][0], venue_id='b', pub_name='Second Inn'))
        bundle['locations']['b'] = copy.deepcopy(bundle['locations']['a'])
        registry['venues']['b'] = copy.deepcopy(registry['venues']['a'])
        registry['venues']['a']['verified_pin']['review_required'] = True
        original = build_queue(bundle, registry, report)
        target = next(q for q in original if q['venue_id'] == 'b')
        for category in ('named_place_pin_conflict', 'named_place_identity_conflict', 'trading_status_conflict'):
            receipt = make_receipt(target, category, '2026-10-31', 'reviewer',
                                   'https://www.google.com/maps/place/Second+Inn')
            queue, summary = apply_receipts(original, [receipt])
            self.assertEqual(queue[0]['venue_id'], 'b')
            self.assertEqual(queue[0]['risk_rank'], 1)
            self.assertIn('google_observation_conflict', queue[0]['risk_flags'])
            self.assertEqual(summary['conflict_venue_count'], 1)
            self.assertEqual(queue[0]['current_pin'], target['current_pin'])
            self.assertFalse(queue[0]['google_verified'])


if __name__ == '__main__':
    unittest.main()
