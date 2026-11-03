import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from audit_google_places import (CONFIGURATION_ISSUE, COVERAGE_ISSUE, ENDPOINT,
                                 FIELD_MASK, ProviderError, audit, compare, fresh,
                                 load_state, main, search_places)
from build_place_review_queue import build_queue
from directory_release import read_json, write_json
from tests.test_place_review_queue import fixture


def result(name='Example Inn', postcode='BS1 1AA', lat=51.45, lng=-2.59, business='OPERATIONAL', identifier='ChIJ-example'):
    return {'places': [dict(id=identifier, displayName={'text': name},
                            addressComponents=[dict(types=['postal_code'], longText=postcode)],
                            location=dict(latitude=lat, longitude=lng), businessStatus=business)]}


class GooglePlacesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'published'
        self.state = self.base / 'state.json'
        self.now = datetime(2026, 11, 2, 7, tzinfo=timezone.utc)
        self.bundle, self.registry, self.report = fixture()
        self.write_inputs()

    def write_inputs(self):
        (self.root / 'audit').mkdir(parents=True, exist_ok=True)
        write_json(self.root / 'directory-release.json', self.bundle)
        write_json(self.root / 'venue-registry.json', self.registry)
        write_json(self.root / 'audit/full-location-audit.json', self.report)
        self.before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def run_audit(self, lookup=None, **kwargs):
        settings = dict(enabled=True, api_key='secret-fixture-key', lookup=lookup or Mock(return_value=result()), now=self.now)
        settings.update(kwargs)
        status = audit(self.root, self.state, **settings)
        self.assertEqual(self.before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        return status

    def test_both_opt_in_and_key_required_and_no_false_pass(self):
        for enabled, key in ((False, ''), (False, 'key'), (True, ''), (True, ' ')):
            lookup = Mock(side_effect=AssertionError('Network must remain off'))
            status = self.run_audit(lookup, enabled=enabled, api_key=key)
            lookup.assert_not_called()
            self.assertFalse(status['configured'])
            self.assertIn(CONFIGURATION_ISSUE, status['issues'])
            self.assertIn(COVERAGE_ISSUE, status['issues'])
            self.assertEqual(status['counts']['current'], 0)
            self.assertEqual(status['counts']['requests_this_run'], 0)
            self.assertFalse(status['hard_failure'])

    def test_real_comparison_does_not_persist_google_content_or_change_map(self):
        response = result()
        response['places'][0]['formattedAddress'] = 'DO_NOT_PERSIST_GOOGLE_ADDRESS'
        status = self.run_audit(Mock(return_value=response))
        self.assertFalse(status['issues'])
        self.assertEqual(status['counts']['current'], 1)
        self.assertFalse(status['findings'])
        saved = self.state.read_text()
        self.assertNotIn('DO_NOT_PERSIST', saved)
        self.assertNotIn('secret-fixture-key', saved)
        self.assertNotIn('location', saved)
        self.assertNotIn('displayName', saved)
        self.assertNotIn('businessStatus', saved)
        self.assertNotIn('latitude', saved)
        self.assertEqual(load_state(self.state)['checks']['a']['place_id'], 'ChIJ-example')

    def test_coordinate_and_trading_disputes_are_review_only(self):
        status = self.run_audit(Mock(return_value=result(lat=51.46, business='CLOSED_TEMPORARILY')))
        self.assertEqual(len(status['findings']), 1)
        self.assertIn('more than 50 metres', status['findings'][0]['reason'])
        self.assertIn('possible closure', status['findings'][0]['reason'])
        self.assertEqual(set(status['findings'][0]), {'venue_id', 'pub_name', 'place_name', 'postcode', 'status', 'reason', 'binding_signature'})
        self.assertNotIn('lat', status['findings'][0])
        self.assertNotIn('lng', status['findings'][0])

    def test_wrong_name_postcode_multiple_matches_and_truncated_results_cannot_confirm(self):
        item = build_queue(self.bundle, self.registry, self.report)[0]
        cases = [result(name='Other Inn'), result(postcode='BS1 1AB'), {'places': []},
                 dict(result(), nextPageToken='more'),
                 {'places': result()['places'] + result(identifier='other-id')['places']}]
        for response in cases:
            with self.subTest(response=response):
                self.assertEqual(compare(item, self.registry['venues']['a'], response)['status'], 'review_needed')

    def test_approximate_invalid_points_and_unknown_trading_stay_review_needed(self):
        item = build_queue(self.bundle, self.registry, self.report)[0]
        for point in (dict(latitude=True, longitude=-2.59), dict(latitude=float('nan'), longitude=-2.59), {}, None):
            response = result()
            response['places'][0]['location'] = point
            self.assertEqual(compare(item, self.registry['venues']['a'], response)['status'], 'review_needed')
        self.assertEqual(compare(dict(item, precision='postcode'), self.registry['venues']['a'], result())['status'], 'review_needed')
        self.assertEqual(compare(item, self.registry['venues']['a'], result(business=None))['status'], 'review_needed')

    def test_reference_postcode_and_name_must_have_known_source_provenance(self):
        item = build_queue(self.bundle, self.registry, self.report)[0]
        entry = copy.deepcopy(self.registry['venues']['a'])
        evidence = entry['verified_pin']['evidence'][0]
        evidence.update(reference_name='Renamed Inn', reference_postcode='BS1 1AB')
        self.assertEqual(compare(item, entry, result(name='Renamed Inn', postcode='BS1 1AB'))['status'], 'consistent_within_50m')
        evidence['url'] = 'https://maps.google.co.uk/maps/place/Renamed'
        self.assertEqual(compare(item, entry, result(name='Renamed Inn', postcode='BS1 1AB'))['status'], 'review_needed')

    def test_rotation_and_usage_reserved_before_every_request(self):
        for identifier in ('b', 'c'):
            self.bundle['rows'].append(dict(self.bundle['rows'][0], venue_id=identifier))
            self.bundle['locations'][identifier] = copy.deepcopy(self.bundle['locations']['a'])
            self.registry['venues'][identifier] = copy.deepcopy(self.registry['venues']['a'])
        self.write_inputs()
        seen = []
        def lookup(item, key):
            seen.append(item['venue_id'])
            self.assertEqual(read_json(self.state)['usage']['2026-11-02'], len(seen))
            return result()
        self.run_audit(lookup, limit=2)
        self.run_audit(lookup, limit=1, now=self.now + timedelta(hours=1))
        self.assertEqual(len(set(seen)), 3)
        self.assertEqual(load_state(self.state)['usage']['2026-11-02'], 3)

    def test_identity_or_point_changes_invalidate_prior_check_and_thirty_days_expire(self):
        self.run_audit()
        original = load_state(self.state)['checks']['a']
        item = build_queue(self.bundle, self.registry, self.report)[0]
        self.assertTrue(fresh(original, item, self.now + timedelta(days=30)))
        self.assertFalse(fresh(original, item, self.now + timedelta(days=31)))
        self.assertFalse(fresh(original, item, self.now - timedelta(seconds=1)))
        for field in ('pub_name', 'place_name', 'postcode'):
            changed = copy.deepcopy(self.bundle)
            changed['rows'][0][field] += ' changed'
            self.assertFalse(fresh(original, build_queue(changed, self.registry, self.report)[0], self.now))
        self.bundle['locations']['a']['lat'] += 0.01
        self.write_inputs()
        status = self.run_audit(enabled=False)
        self.assertEqual(status['counts']['current'], 0)

    def test_provider_failure_has_no_retry_no_secret_and_retains_previous_finding(self):
        self.run_audit(Mock(return_value=result(lat=51.46)))
        previous = load_state(self.state)['checks']['a']['checked_at']
        lookup = Mock(side_effect=RuntimeError('secret-fixture-key DO_NOT_PERSIST_GOOGLE_ERROR'))
        status = self.run_audit(lookup, now=self.now + timedelta(days=1))
        self.assertTrue(status['hard_failure'])
        lookup.assert_called_once()
        self.assertEqual(load_state(self.state)['checks']['a']['checked_at'], previous)
        self.assertEqual(len(status['findings']), 1)
        self.assertNotIn('secret-fixture-key', json.dumps(status))
        self.assertNotIn('DO_NOT_PERSIST', self.state.read_text())
        status = self.run_audit(Mock(side_effect=ProviderError('secret_fixture_key')), now=self.now + timedelta(days=2))
        self.assertNotIn('secret_fixture_key', json.dumps(status))
        self.assertIn('lookup_failed', '; '.join(status['issues']))

    def test_limits_stop_before_network_and_corrupt_usage_fails_closed(self):
        for usage in ({'2026-11-02': 45}, {'2026-11-01': 1400}):
            write_json(self.state, dict(schema_version=1, checks={}, usage=usage))
            lookup = Mock()
            status = self.run_audit(lookup)
            lookup.assert_not_called()
            self.assertIn('request cap reached', '; '.join(status['issues']))
        write_json(self.state, dict(schema_version=1, checks={}, usage={'2026-11-02': -1}))
        with self.assertRaises(ValueError):
            self.run_audit()
        for limit in (-1, 46):
            with self.assertRaises(ValueError):
                self.run_audit(limit=limit)

    def test_cli_missing_configuration_and_bad_state_emit_durable_status(self):
        status_path = self.base / 'status.json'
        args = ['--root', str(self.root), '--state', str(self.state), '--status', str(status_path)]
        with patch.dict(os.environ, {'AUDIT_RUN_ID': '7', 'AUDIT_RUN_ATTEMPT': '2'}, clear=True), patch('builtins.print'):
            self.assertEqual(main(args), 0)
            self.assertEqual(read_json(status_path)['run_attempt'], 2)
            write_json(self.state, {'schema_version': 999})
            self.assertEqual(main(args), 1)
        self.assertEqual(read_json(status_path)['run_id'], '7')
        self.assertTrue(read_json(status_path)['hard_failure'])

    def test_client_uses_one_fixed_post_with_bounded_fields_and_no_bias_to_existing_pin(self):
        item = build_queue(self.bundle, self.registry, self.report)[0]
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(result()).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch('audit_google_places.build_opener', return_value=opener):
            self.assertEqual(search_places(item, 'key'), result())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.get_header('X-goog-fieldmask'), FIELD_MASK)
        body = json.loads(request.data)
        self.assertEqual(body['pageSize'], 5)
        self.assertNotIn('locationBias', body)
        self.assertNotIn('pageToken', body)
        opener.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
