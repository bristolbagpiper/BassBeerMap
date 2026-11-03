"""Inactive source history must never regain authority over a current pin."""
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_map_health import apply_check, health_audit
from audit_google_places import compare
from build_place_review_queue import build_queue
from coordinate_sources import name_matches, recheck_pin
from directory_release import (active_evidence, build_bundle, build_ledger, evidence_valid,
                               read_json, write_release)
from tests.test_directory_release import pin, registry_for, row


class ActiveEvidenceTests(unittest.TestCase):
    def fixture(self):
        listing = row()
        registry = registry_for([listing])
        entry = registry['venues'][listing['venue_id']]
        bundle = dict(rows=[listing], locations={listing['venue_id']: dict(lat=51.45, lng=-2.59, precision='venue')})
        return listing, entry, registry, bundle

    def camra_page(self, name='Example Inn', postcode='BS1 1AA', lat=51.45):
        return '<script type="application/ld+json">' + json.dumps(dict(
            **{'@type': 'BarOrPub'}, name=name, geo=dict(latitude=lat, longitude=-2.59),
            address=dict(streetAddress='1 Current Street', addressLocality='Bristol', postalCode=postcode))) + '</script>'

    def place(self, name='Example Inn', postcode='BS1 1AA'):
        return dict(places=[dict(id='test-place', displayName=dict(text=name),
                    addressComponents=[dict(types=['postal_code'], longText=postcode)],
                    location=dict(latitude=51.45, longitude=-2.59), businessStatus='OPERATIONAL')])

    def test_explicit_dispositions_fail_closed_without_removing_history(self):
        current = pin()['evidence'][0]
        historical = [dict(current, disposition=value) for value in (
            'superseded_by_owner_review', 'historical', 'rejected', 'noncurrent', 'unknown_future_value', '', None, [])]
        stored = dict(pin(), evidence=historical + [current])
        before = copy.deepcopy(stored)
        self.assertEqual(active_evidence(stored), [current])
        self.assertTrue(evidence_valid(stored))
        self.assertEqual(stored, before)
        for item in historical:
            with self.subTest(disposition=item['disposition']):
                self.assertFalse(evidence_valid(dict(stored, evidence=[item])))
        for malformed in (None, [], {'evidence': None}, {'evidence': {}}, {'evidence': ['not an evidence record']}):
            self.assertEqual(active_evidence(malformed), [])

    def test_fresh_superseded_evidence_cannot_refresh_expired_active_evidence(self):
        today = date.today()
        old = dict(pin()['evidence'][0], checked_at=(today - timedelta(days=401)).isoformat())
        recent_history = dict(old, checked_at=today.isoformat(), disposition='superseded_by_owner_review')
        stored = dict(pin(), evidence=[recent_history, old])
        self.assertFalse(evidence_valid(stored, today=today))
        self.assertTrue(evidence_valid(stored, today=today, allow_expired=True))
        self.assertFalse(evidence_valid(dict(stored, evidence=[recent_history]), today=today, allow_expired=True))

    def test_chestnuts_current_manual_pin_remains_valid_and_does_not_fetch_superseded_camra(self):
        listing = row('Chestnuts (PMC)', 'CV10 0AB')
        listing['place_name'] = 'Nuneaton: Central'
        registry = registry_for([listing])
        entry = registry['venues'][listing['venue_id']]
        current = dict(source='manual_named_place_map', checked_at=date.today().isoformat(),
                       url='https://example.test/owner-review', reference_name='The Chestnuts Club Ltd',
                       reference_postcode='CV10 0AB', reference_coordinate=dict(lat=52.5275151, lng=-1.4611907))
        superseded = dict(current, source='camra', url='https://camra.org.uk/pubs/196934',
                          reference_coordinate=dict(lat=52.527003175492, lng=-1.460884957008),
                          disposition='superseded_by_owner_review')
        entry['verified_pin'] = dict(lat=52.5275151, lng=-1.4611907, source='manual_named_place_map',
                                     evidence=[superseded, current])
        before = copy.deepcopy(entry['verified_pin'])
        with patch('coordinate_sources.fetch_text', side_effect=AssertionError('Inactive CAMRA must not be fetched')) as fetch:
            self.assertEqual(recheck_pin(listing, entry['verified_pin'])['status'], 'manual_review')
        fetch.assert_not_called()
        ledger = build_ledger([listing], registry)
        record = next(iter(ledger['records'].values()))
        self.assertEqual(record['display_precision'], 'venue')
        self.assertEqual(record['evidence'], before['evidence'])
        self.assertEqual(entry['verified_pin'], before)

    def test_active_camra_is_rechecked_after_inactive_history_and_real_conflicts_remain(self):
        listing = row()
        current = pin()['evidence'][0]
        stored = dict(pin(), evidence=[dict(current, url='https://camra.org.uk/pubs/old',
                                           disposition='superseded_by_owner_review'), current])
        before = copy.deepcopy(stored)
        with patch('coordinate_sources.fetch_text', return_value=self.camra_page()) as fetch:
            self.assertEqual(recheck_pin(listing, stored)['status'], 'confirmed')
        fetch.assert_called_once_with(current['url'])
        for page, reason in ((self.camra_page(name='Different Pub'), 'Reference venue name changed'),
                             (self.camra_page(postcode='BS1 1AB'), 'Reference address/postcode changed'),
                             (self.camra_page(lat=51.46), 'Reference coordinate changed by more than 50 metres')):
            with self.subTest(reason=reason), patch('coordinate_sources.fetch_text', return_value=page):
                result = recheck_pin(listing, stored)
                self.assertEqual((result['status'], result['reason']), ('conflict', reason))
        self.assertEqual(stored, before)

    def test_newest_accepted_camra_reference_is_used_instead_of_first_unnamed_reading(self):
        examples = [('Shepherds Hall Ale House', "Shepherds' Hall Ale House & Victoria Rooms, Chorley"),
                    ('Stapenhill Institute', 'Stapenhill Institute Club, Burton upon Trent')]
        for listing_name, accepted_name in examples:
            listing = row(listing_name)
            older = dict(pin()['evidence'][0], checked_at=(date.today() - timedelta(days=20)).isoformat())
            accepted = dict(older, checked_at=(date.today() - timedelta(days=4)).isoformat(),
                            reference_name=accepted_name, reference_address='1 Current Street',
                            reviewed_by='repository-owner', match='owner_confirmed_named_reference')
            for readings in ([older, accepted], [accepted, older], [dict(older, checked_at=accepted['checked_at']), accepted]):
                with self.subTest(listing=listing_name, readings=readings):
                    stored = dict(pin(), evidence=readings)
                    before = copy.deepcopy(stored)
                    with patch('coordinate_sources.fetch_text', return_value=self.camra_page(name=accepted_name)):
                        result = recheck_pin(listing, stored)
                    self.assertEqual(result['status'], 'confirmed')
                    self.assertEqual(result['evidence']['match'], accepted['match'])
                    self.assertEqual(result['evidence']['reviewed_by'], 'repository-owner')
                    self.assertEqual(stored, before)

    def test_an_old_reviewed_title_cannot_mask_a_rename_after_newer_accepted_evidence(self):
        older = dict(pin()['evidence'][0], checked_at=(date.today() - timedelta(days=20)).isoformat(),
                     reference_name='Former Accepted Alias')
        accepted = dict(older, checked_at=(date.today() - timedelta(days=4)).isoformat(),
                        reference_name='Latest Accepted Alias', reviewed_by='repository-owner')
        stored = dict(pin(), evidence=[older, accepted])
        for name in ('Former Accepted Alias', 'New Unreviewed Name'):
            with self.subTest(name=name), patch('coordinate_sources.fetch_text', return_value=self.camra_page(name=name)):
                result = recheck_pin(row(), stored)
                self.assertEqual((result['status'], result['reason']), ('conflict', 'Reference venue name changed'))
        for page, reason in ((self.camra_page(name=accepted['reference_name'], postcode='BS1 1AB'), 'Reference address/postcode changed'),
                             (self.camra_page(name=accepted['reference_name'], lat=51.46), 'Reference coordinate changed by more than 50 metres')):
            with self.subTest(reason=reason), patch('coordinate_sources.fetch_text', return_value=page):
                result = recheck_pin(row(), stored)
                self.assertEqual((result['status'], result['reason']), ('conflict', reason))

    def test_refresh_preserves_owner_history_and_archives_old_active_readings_once(self):
        old = dict(pin()['evidence'][0], checked_at=(date.today() - timedelta(days=1)).isoformat(),
                   reference_name='Previous source title')
        owner_history = dict(old, disposition='superseded_by_owner_review', reference_name='Rejected owner source')
        unrelated = dict(old, source='manual_named_place_map', url='https://example.test/owner-review')
        entry = dict(verified_pin=dict(pin(), evidence=[owner_history, old, unrelated]))
        original = copy.deepcopy(entry)
        current = dict(old, checked_at=date.today().isoformat(), reference_name='Current accepted source title')
        result = dict(status='confirmed', evidence=current)
        result_before = copy.deepcopy(result)
        apply_check(entry, result)
        history = entry['verified_pin']['evidence']
        self.assertIn(owner_history, history)
        self.assertEqual(history[0], original['verified_pin']['evidence'][0])
        self.assertIn(unrelated, active_evidence(entry['verified_pin']))
        self.assertIn(dict(old, disposition='superseded_by_source_refresh'), history)
        self.assertNotIn(old, active_evidence(entry['verified_pin']))
        self.assertEqual([item for item in active_evidence(entry['verified_pin']) if item['url'] == current['url']], [current])
        self.assertEqual({k: entry['verified_pin'][k] for k in ('lat', 'lng')},
                         {k: original['verified_pin'][k] for k in ('lat', 'lng')})
        after = copy.deepcopy(entry)
        apply_check(entry, result)
        self.assertEqual(entry, after)
        self.assertEqual(result, result_before)

    def test_refresh_cannot_reactivate_an_explicitly_inactive_result(self):
        entry = dict(verified_pin=pin())
        before = copy.deepcopy(entry)
        with self.assertRaisesRegex(ValueError, 'must be active'):
            apply_check(entry, dict(status='confirmed', evidence=dict(pin()['evidence'][0], disposition='historical')))
        self.assertEqual(entry, before)

    def test_health_audit_reports_only_the_checked_source_url_and_preserves_other_evidence(self):
        listing, entry, registry, _ = self.fixture()
        camra = entry['verified_pin']['evidence'][0]
        manual = dict(camra, source='manual_named_place_map', url='https://www.google.com/maps/place/Example-Inn')
        fsa = dict(camra, source='food-standards-agency-fhrs', url='https://ratings.food.gov.uk/business/123')
        owner_history = dict(camra, disposition='superseded_by_owner_review', reference_name='Historical rejected name')
        entry['verified_pin']['evidence'] = [owner_history, camra, manual, fsa]
        before_pin = copy.deepcopy(entry['verified_pin'])
        cases = [('confirmed', dict(status='confirmed', evidence=dict(camra, reference_name='Example Inn'))),
                 ('conflict', dict(status='conflict', reason='Reference venue name changed', source_url=camra['url'])),
                 ('unavailable', dict(status='unavailable', reason='TimeoutError')),
                 ('manual_review', dict(status='manual_review', reason='Manual address/map check needed'))]
        for overall, result in cases:
            with self.subTest(overall=overall), tempfile.TemporaryDirectory() as directory:
                root, stage = Path(directory) / 'published', Path(directory) / 'candidate'
                write_release(root, [listing], copy.deepcopy(registry), {}, {'BS1 1AA': dict(lat=51.45, lng=-2.59)})
                source_files = {path.name: path.read_bytes() for path in root.iterdir()}
                with patch('audit_map_health.recheck_pin', return_value=result), patch('audit_map_health.time.sleep'):
                    report = health_audit(root, stage, limit=1, check_live=False)
                self.assertTrue(report['valid'], report['errors'])
                updated = read_json(stage / 'venue-registry.json')['venues'][listing['venue_id']]
                sources = updated['verification_attempt']['sources']
                self.assertEqual(updated['verification_attempt']['status'], overall)
                self.assertEqual(sources['camra']['status'], overall if overall in ('confirmed', 'conflict') else 'not_checked')
                self.assertEqual(sources['manual_named_place_map']['status'], 'not_checked')
                self.assertEqual(sources['food-standards-agency-fhrs']['status'], 'not_checked')
                self.assertIn(owner_history, updated['verified_pin']['evidence'])
                self.assertIn(manual, updated['verified_pin']['evidence'])
                self.assertIn(fsa, updated['verified_pin']['evidence'])
                self.assertEqual({k: updated['verified_pin'][k] for k in ('lat', 'lng')},
                                 {k: before_pin[k] for k in ('lat', 'lng')})
                self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, source_files)

    def test_only_explicit_trailing_alias_annotation_is_removed_from_primary_name(self):
        self.assertTrue(name_matches('New Baron', 'New Baron Hinckley (also known as Crafty Baron)', 'Hinckley'))
        self.assertTrue(name_matches('New Baron', 'New Baron Hinckley (also known as Crafty Baron), Hinckley - Elmesthorpe Ales', 'Hinckley'))
        for reference in ('Different Pub (also known as New Baron Hinckley)',
                          'Different Pub (formerly New Baron Hinckley)',
                          'New Baron Nuneaton (also known as New Baron Hinckley)',
                          'New Baron Hinckley (North)',
                          'New Baron Hinckley (formerly Crafty Baron)',
                          'New Baron Hinckley (Crafty Baron)',
                          'New Baron Hinckley (also known as Crafty Baron) North',
                          'New Baron Hinckley (also known as Crafty Baron (North))'):
            with self.subTest(reference=reference):
                self.assertFalse(name_matches('New Baron', reference, 'Hinckley'))

    def test_leading_ye_article_and_existing_inn_suffix_match_without_broadening_club_names(self):
        self.assertTrue(name_matches('Ye Olde Rock', 'Olde Rock Inn, Upper Hulme - 17th Century Inn', 'Upper Hulme'))
        self.assertTrue(name_matches('Olde Rock', 'Ye Olde Rock Inn', 'Upper Hulme'))
        self.assertFalse(name_matches('Ye Olde Rock', 'New Rock Inn', 'Upper Hulme'))
        self.assertFalse(name_matches('Chestnuts (PMC)', 'Chestnuts Social Club, Nuneaton', 'Nuneaton: Central'))

    def test_formatter_acceptance_cannot_bypass_postcode_or_coordinate_checks(self):
        fixtures = [('New Baron', 'Hinckley', 'New Baron Hinckley (also known as Crafty Baron)'),
                    ('Ye Olde Rock', 'Upper Hulme', 'Olde Rock Inn, Upper Hulme - 17th Century Inn')]
        for listing_name, locality, reference in fixtures:
            listing = dict(row(listing_name), place_name=locality)
            for page, expected in ((self.camra_page(name=reference), 'confirmed'),
                                   (self.camra_page(name=reference, postcode='BS1 1AB'), 'Reference address/postcode changed'),
                                   (self.camra_page(name=reference, lat=51.46), 'Reference coordinate changed by more than 50 metres')):
                with self.subTest(listing=listing_name, expected=expected), patch('coordinate_sources.fetch_text', return_value=page):
                    result = recheck_pin(listing, pin())
                    if expected == 'confirmed':
                        self.assertEqual(result['status'], 'confirmed')
                    else:
                        self.assertEqual((result['status'], result['reason']), ('conflict', expected))

    def test_active_fsa_fallback_is_used_after_superseded_camra(self):
        historical = dict(pin()['evidence'][0], disposition='superseded_by_owner_review')
        current = dict(pin()['evidence'][0], source='food-standards-agency-fhrs',
                       url='https://ratings.food.gov.uk/business/123')
        stored = dict(pin(), evidence=[historical, current])
        replacement = dict(pin(), evidence=[dict(current, match='refreshed')])
        with (patch('coordinate_sources.fetch_text', side_effect=AssertionError('Inactive CAMRA fetched')) as fetch,
              patch('coordinate_sources.resolve_new_pin', return_value=replacement) as resolve):
            result = recheck_pin(row(), stored)
        fetch.assert_not_called()
        resolve.assert_called_once()
        self.assertEqual(result, dict(status='confirmed', evidence=replacement['evidence'][0]))

    def test_published_check_date_uses_active_evidence_while_ledger_retains_history(self):
        listing, entry, registry, _ = self.fixture()
        current = entry['verified_pin']['evidence'][0]
        historical = dict(current, checked_at=(date.today() + timedelta(days=10)).isoformat(), disposition='historical')
        entry['verified_pin']['evidence'].insert(0, historical)
        ledger = build_ledger([listing], registry)
        bundle = build_bundle([listing], {}, {'BS1 1AA': dict(lat=51.45, lng=-2.59)}, ledger)
        self.assertEqual(ledger['generated_at'], current['checked_at'])
        self.assertEqual(bundle['locations'][listing['venue_id']]['checked_at'], current['checked_at'])
        self.assertEqual(next(iter(ledger['records'].values()))['evidence'], [historical, current])

    def test_google_search_addresses_aliases_and_postcodes_exclude_superseded_sources(self):
        listing, entry, registry, bundle = self.fixture()
        active = dict(entry['verified_pin']['evidence'][0], reference_name='Current Alias',
                      reference_address='1 Current Street')
        historical = dict(active, disposition='superseded_by_owner_review', reference_name='Rejected Alias',
                          reference_address='99 Rejected Street', reference_postcode='BS1 1AB',
                          url='https://camra.org.uk/pubs/rejected')
        entry['verified_pin']['evidence'] = [historical, active]
        before = copy.deepcopy(registry)
        item = build_queue(bundle, registry, {})[0]
        self.assertIn('1 Current Street', item['google_named_search_query'])
        self.assertNotIn('Rejected', json.dumps(item))
        self.assertNotIn(historical['url'], item['source_urls'])
        self.assertNotIn('BS1 1AB', json.dumps(item['reference_addresses']))
        self.assertEqual(compare(item, entry, self.place(name='Current Alias'))['status'], 'consistent_within_50m')
        for result in (self.place(name='Rejected Alias'), self.place(postcode='BS1 1AB')):
            with self.subTest(result=result):
                self.assertEqual(compare(item, entry, result)['status'], 'review_needed')
        self.assertEqual(registry, before)


if __name__ == '__main__':
    unittest.main()
