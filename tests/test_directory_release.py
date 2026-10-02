import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from audit_map_health import apply_check, health_audit, select_batch
from coordinate_sources import name_matches, parse_camra, recheck_pin, resolve_new_pin
from directory_release import (build_bundle, build_ledger, permanent_id, read_json,
                               reconcile, validate_release, venue_key, write_json, write_release, write_rows)
from ingest_directory import stage_import


def row(name='Example Inn', postcode='BS1 1AA'):
    return dict(country='England', area='Somerset', pub_name=name, place_name='Bristol', postcode=postcode,
                pg='Perm', last='2026 Q3', dispense='H', notes='')


def pin():
    return {'lat': 51.45, 'lng': -2.59, 'source': 'manual-review', 'evidence': [{
        'source': 'camra', 'url': 'https://camra.org.uk/pubs/example-123', 'checked_at': date.today().isoformat(),
        'reference_coordinate': {'lat': 51.45, 'lng': -2.59}, 'reference_postcode': 'BS1 1AA'}]}


def registry_for(rows):
    venues = {}
    for listing in rows:
        key = venue_key(listing)
        identifier = permanent_id(key)
        listing['venue_id'] = identifier
        venues[identifier] = {'aliases': [key], 'current_key': key, 'listing': copy.deepcopy(listing), 'verified_pin': pin()}
    return {'schema_version': 1, 'venues': venues}


class IdentityTests(unittest.TestCase):
    def test_reviewed_postcode_correction_preserves_permanent_id_and_pin(self):
        before = row('GJ Muckers', 'SK11 6JL')
        registry = registry_for([before])
        after = dict(before, postcode='SK11 7NE')
        decision = {'identity_aliases': {venue_key(after): {
            'previous_key': venue_key(before), 'reason': 'same named address and physical location',
            'source_url': 'https://camra.org.uk/pubs/example', 'checked_at': date.today().isoformat(), 'reference_postcode': 'SK11 6JL'}}}
        rows, updated, report = reconcile([after], registry, decision)
        self.assertEqual(report['errors'], [])
        self.assertEqual(rows[0]['venue_id'], before['venue_id'])
        self.assertEqual(updated['venues'][before['venue_id']]['verified_pin']['lat'], 51.45)
        self.assertEqual(report['changes'][0]['type'], 'identity_change')

    def test_unreviewed_postcode_change_is_blocked(self):
        before = row()
        registry = registry_for([before])
        _, _, report = reconcile([dict(before, postcode='BS1 1AB')], registry, {})
        self.assertIn('identity review', report['errors'][0])

    def test_possible_rename_at_same_address_is_blocked(self):
        before = row()
        registry = registry_for([before])
        _, _, report = reconcile([row('New Name')], registry, {})
        self.assertIn('identity review', report['errors'][0])

    def test_conflicting_duplicate_is_blocked_and_reviewed_exception_cannot_drift(self):
        one, two = row('Black Lion'), dict(row('Black Lion'), pg='Guest', last='2026 Q2')
        _, _, report = reconcile([one, two], {'venues': {}}, {})
        self.assertIn('conflicting duplicate', report['errors'][0])
        decision = {'duplicate_resolutions': {venue_key(one): {'accepted': {'pg': 'Perm', 'last': '2026 Q3'},
                    'rejected': [{'pg': 'Guest', 'last': '2026 Q2'}], 'source_url': 'https://example.test/pdf',
                    'reason': 'reviewed conflicting older record', 'checked_at': date.today().isoformat()}}}
        rows, _, report = reconcile([one, two], {'venues': {}}, decision)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['errors'], [])
        _, _, report = reconcile([one, dict(two, last='2026 Q4')], {'venues': {}}, decision)
        self.assertIn('beyond the reviewed exception', report['errors'][0])

    def test_distinct_new_pub_gets_own_id_and_no_verified_pin(self):
        old = row()
        registry = registry_for([old])
        rows, updated, report = reconcile([old, row('Other Inn')], registry, {})
        self.assertEqual(report['errors'], [])
        self.assertNotEqual(rows[0]['venue_id'], rows[1]['venue_id'])
        self.assertIsNone(updated['venues'][rows[1]['venue_id']]['verified_pin'])


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'published'
        self.stage = Path(self.temporary.name) / 'candidate'
        self.rows = [row(f'Example Inn {i}') for i in range(20)]
        self.registry = registry_for(self.rows)
        self.metadata = {'directory_label': 'Test directory'}
        self.postcodes = {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}}
        write_release(self.root, self.rows, self.registry, self.metadata, self.postcodes)

    def tearDown(self):
        self.temporary.cleanup()

    def test_complete_consistent_release_passes(self):
        self.assertEqual(validate_release(self.root), [])

    def test_same_exact_point_cannot_be_reused_for_different_postcodes(self):
        rows = copy.deepcopy(self.rows)
        rows[-1]['postcode'] = 'BS1 1AB'
        registry = registry_for(rows)
        registry['venues'][rows[-1]['venue_id']]['verified_pin']['evidence'][0]['reference_postcode'] = 'BS1 1AB'
        write_release(self.stage, rows, registry, self.metadata, dict(self.postcodes, **{'BS1 1AB': {'lat': 51.46, 'lng': -2.58}}))
        self.assertTrue(any('exact point is reused' in error for error in validate_release(self.stage)))

    def test_wrong_address_evidence_cannot_verify_a_venue(self):
        registry = copy.deepcopy(self.registry)
        registry['venues'][self.rows[0]['venue_id']]['verified_pin']['evidence'][0]['reference_postcode'] = 'BS9 9ZZ'
        write_release(self.stage, self.rows, registry, self.metadata, self.postcodes)
        self.assertTrue(any('different address' in error for error in validate_release(self.stage)))

    def test_corrupt_ledger_or_map_bundle_is_rejected(self):
        path = self.root / 'coordinate-verification.json'
        ledger = read_json(path)
        ledger['records'][venue_key(self.rows[0])]['lat'] = 55
        write_json(path, ledger)
        self.assertTrue(any('ledger' in error for error in validate_release(self.root)))
        write_release(self.root, self.rows, self.registry, self.metadata, self.postcodes)
        path = self.root / 'directory-release.json'
        bundle = read_json(path)
        bundle['rows'].pop()
        write_json(path, bundle)
        self.assertTrue(any('Map release' in error for error in validate_release(self.root)))

    def test_verified_coordinate_move_requires_review_even_with_fresh_evidence(self):
        registry = copy.deepcopy(self.registry)
        point = registry['venues'][self.rows[0]['venue_id']]['verified_pin']
        point['lat'] += 0.02
        point['evidence'][0]['reference_coordinate']['lat'] += 0.02
        write_release(self.stage, self.rows, registry, self.metadata, self.postcodes)
        errors = validate_release(self.stage, self.root)
        self.assertTrue(any('moved more than 50' in error for error in errors))

    def test_source_outage_allows_new_approximate_pub_without_moving_existing_pins(self):
        candidate = Path(self.temporary.name) / 'raw.csv'
        write_rows(candidate, self.rows + [dict(row('New Inn'), venue_id='')])
        before = (self.root / 'directory-release.json').read_bytes()
        with patch('ingest_directory.resolve_new_pin', side_effect=TimeoutError('source offline')):
            report = stage_import(candidate, self.root, self.stage)
        self.assertTrue(report['valid'], report['errors'])
        self.assertEqual(report['summary']['approximate_pins'], 1)
        self.assertEqual(report['summary']['added_count'], 1)
        self.assertEqual((self.root / 'directory-release.json').read_bytes(), before)

    def test_failed_import_leaves_published_release_untouched(self):
        candidate = Path(self.temporary.name) / 'raw.csv'
        write_rows(candidate, self.rows + [dict(self.rows[0], pg='Guest')])
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        report = stage_import(candidate, self.root, self.stage, check_sources=False)
        self.assertFalse(report['valid'])
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_expired_evidence_never_produces_a_precise_pin(self):
        registry = copy.deepcopy(self.registry)
        registry['venues'][self.rows[0]['venue_id']]['verified_pin']['evidence'][0]['checked_at'] = (date.today() - timedelta(days=401)).isoformat()
        ledger = build_ledger(self.rows, registry)
        self.assertEqual(ledger['records'][venue_key(self.rows[0])]['display_precision'], 'postcode')

    def test_weekly_confirmed_source_conflict_quarantines_pin_without_guessing_a_move(self):
        with patch('audit_map_health.recheck_pin', return_value={'status': 'conflict', 'reason': 'Source moved by 400 metres'}), patch('audit_map_health.time.sleep'):
            report = health_audit(self.root, self.stage, limit=1, check_live=False)
        self.assertTrue(report['valid'], report['errors'])
        self.assertTrue(report['notify'])
        locations = read_json(self.stage / 'directory-release.json')['locations']
        self.assertEqual(sum(p['precision'] == 'postcode' for p in locations.values()), 1)
        self.assertEqual(validate_release(self.stage, self.root, allow_review_downgrades=True), [])
        self.assertTrue(any('lost verification' in e for e in validate_release(self.stage, self.root)))

    def test_weekly_outage_retains_verified_pin_and_evidence_date(self):
        with patch('audit_map_health.recheck_pin', side_effect=TimeoutError('offline')), patch('audit_map_health.time.sleep'):
            report = health_audit(self.root, self.stage, limit=1, check_live=False)
        self.assertTrue(report['valid'])
        self.assertEqual(read_json(self.stage / 'venue-registry.json'), self.registry)
        self.assertTrue(report['source_warnings'])

    def test_successful_unchanged_audit_stays_quiet(self):
        result = {'status': 'confirmed', 'evidence': pin()['evidence'][0]}
        with patch('audit_map_health.recheck_pin', return_value=result), patch('audit_map_health.time.sleep'):
            report = health_audit(self.root, self.stage, limit=1, check_live=False)
        self.assertTrue(report['valid'])
        self.assertFalse(report['notify'])


class SourceEvidenceTests(unittest.TestCase):
    def test_harmless_reference_name_variations_do_not_quarantine_correct_pins(self):
        self.assertTrue(name_matches('Pig and Pump', 'Pig & Pump, Chesterfield - Pub'))
        self.assertTrue(name_matches('Pestle and Mortar', 'Pestle & Mortar, Hinckley'))
        self.assertTrue(name_matches('GJ Muckers', 'G&j Muckers, Macclesfield - Pub'))
        self.assertTrue(name_matches('Barrels', 'Barrels Ale House, Berwick'))
        self.assertFalse(name_matches('Red Lion', 'Red Lion and Crown, Bristol'))

    def test_aged_evidence_can_be_audited_and_quarantined_without_blocking_recovery(self):
        rows = [row()]
        registry = registry_for(rows)
        with tempfile.TemporaryDirectory() as directory:
            root, stage = Path(directory) / 'root', Path(directory) / 'stage'
            write_release(root, rows, registry, {}, {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}})
            old_date = (date.today() - timedelta(days=401)).isoformat()
            # Simulate a release ageing in place; its stored blue status was
            # valid when published. Its browser independently falls back now.
            registry['venues'][rows[0]['venue_id']]['verified_pin']['evidence'][0]['checked_at'] = old_date
            ledger = build_ledger(rows, registry, allow_expired=True)
            write_json(root / 'venue-registry.json', registry)
            write_json(root / 'coordinate-verification.json', ledger)
            write_json(root / 'directory-release.json', build_bundle(rows, {}, {'BS1 1AA': {'lat': 51.46, 'lng': -2.58}}, ledger))
            self.assertEqual(validate_release(root, allow_expired=True), [])
            with patch('audit_map_health.recheck_pin', side_effect=TimeoutError()), patch('audit_map_health.time.sleep'):
                report = health_audit(root, stage, limit=1, check_live=False)
            self.assertTrue(report['valid'], report['errors'])
            self.assertEqual(read_json(stage / 'directory-release.json')['locations'][rows[0]['venue_id']]['precision'], 'postcode')

    def test_new_pin_requires_independent_agreement(self):
        match = {'lat': 51.45, 'lng': -2.59, 'fhrs_id': 123}
        with patch('coordinate_sources.search', return_value=[]), patch('coordinate_sources.choose_match', return_value=match), patch('coordinate_sources.camra_reference', return_value=None):
            self.assertIsNone(resolve_new_pin(row()))
        reference = {'lat': 51.4501, 'lng': -2.59, 'url': 'https://camra.org.uk/pubs/1', 'name': 'Example Inn', 'postcode': 'BS1 1AA'}
        with patch('coordinate_sources.search', return_value=[]), patch('coordinate_sources.choose_match', return_value=match), patch('coordinate_sources.camra_reference', return_value=reference):
            result = resolve_new_pin(row())
        self.assertEqual(result['lat'], reference['lat'])
        self.assertEqual(len(result['evidence']), 2)

    def test_ordinary_successful_html_is_not_coordinate_evidence(self):
        with self.assertRaises(ValueError):
            parse_camra('<html><h1>Welcome</h1></html>')

    def test_disputed_pin_cannot_be_automatically_restored(self):
        entry = {'verified_pin': dict(pin(), review_required=True, review_reason='address conflict')}
        apply_check(entry, {'status': 'confirmed', 'evidence': pin()['evidence'][0]})
        self.assertTrue(entry['verified_pin']['review_required'])

    def test_camra_recheck_requires_matching_identity_address_and_coordinate(self):
        payload = {'@type': 'BarOrPub', 'name': 'Example Inn, Bristol - Pub',
                   'address': {'postalCode': 'BS1 1AA'}, 'geo': {'latitude': 51.45, 'longitude': -2.59}}
        page = '<script type="application/ld+json">' + json.dumps(payload) + '</script>'
        with patch('coordinate_sources.fetch_text', return_value=page):
            result = recheck_pin(row(), pin())
        self.assertEqual(result['status'], 'confirmed')
        payload['geo']['latitude'] = 52
        page = '<script type="application/ld+json">' + json.dumps(payload) + '</script>'
        with patch('coordinate_sources.fetch_text', return_value=page):
            self.assertEqual(recheck_pin(row(), pin())['status'], 'conflict')


if __name__ == '__main__':
    unittest.main()
