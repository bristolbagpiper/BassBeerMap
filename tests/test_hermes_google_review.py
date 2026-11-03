from datetime import datetime, timezone
from pathlib import Path
import unittest

from build_place_review_queue import build_queue
from build_place_review_queue import digest
from directory_release import read_json
from hermes_google_review import build_status
from location_review import with_google


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


class HermesGoogleReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = read_json(ROOT / 'directory-release.json')
        cls.registry = read_json(ROOT / 'venue-registry.json')
        cls.queue = build_queue(cls.bundle, cls.registry, read_json(ROOT / 'audit/full-location-audit.json', {}))
        cls.item = cls.queue[0]

    def observation(self, category='identity_conflict', **changes):
        timestamp = '2026-11-03T10:00:00Z'
        record = dict(schema_version=1, provider='hermes-browser', venue_id=self.item['venue_id'],
                      identity_signature=self.item['identity_signature'], point_signature=self.item['point_signature'],
                      binding_signature=self.item['binding_signature'], category=category,
                      attempted_at=timestamp, checked_at=timestamp,
                      result_url='https://www.google.com/maps/place/Named+Pub/', source_url='')
        record.update(changes)
        return record

    def status(self, observations):
        return build_status(ROOT, dict(schema_version=1, observations=observations), now=NOW)

    def get_bundle(self):
        return read_json(ROOT / 'directory-release.json')

    def test_conflict_finding_is_exact_and_uses_directory_listing(self):
        status = self.status([self.observation()])
        self.assertEqual(status['counts']['current'], 1)
        finding = status['findings'][0]
        self.assertEqual(set(finding), {'venue_id', 'pub_name', 'place_name', 'postcode', 'status', 'reason', 'binding_signature'})
        self.assertEqual(finding['pub_name'], self.item['pub_name'])
        self.assertNotIn('Named Pub', str(status))

    def test_unavailable_attempt_does_not_clear_conflict_or_refresh_success(self):
        old = self.observation(checked_at='2026-10-01T10:00:00Z', attempted_at='2026-10-01T10:00:00Z')
        failed = self.observation('unavailable', attempted_at='2026-11-03T11:00:00Z', checked_at=None,
                                  result_url='', source_url='https://www.google.com/maps/search/?q=pub')
        status = self.status([old, failed])
        self.assertEqual(status['counts']['current'], 0)
        self.assertIn('conflicting named business identity', status['findings'][0]['reason'])
        self.assertTrue(any('unavailable' in issue for issue in status['issues']))

    def test_identity_conflict_is_actionable_and_simultaneous_conflicts_combine(self):
        status = self.status([self.observation(), self.observation('pin_conflict'),
                              self.observation('trading_conflict')])
        self.assertEqual(len(status['findings']), 1)
        reason = status['findings'][0]['reason']
        self.assertIn('conflicting named business identity', reason)
        self.assertIn('differs by more than 50 metres', reason)
        self.assertIn('possible closure', reason)
        report = with_google(dict(records={}), status, self.get_bundle(), now=NOW)
        finding = report['google_findings'][0]
        self.assertTrue(finding['manual_action_required'])
        identity_status = self.status([self.observation()])
        identity_report = with_google(dict(records={}), identity_status, self.get_bundle(), now=NOW)
        self.assertTrue(identity_report['google_findings'][0]['manual_action_required'])
        self.assertEqual(identity_report['google_findings'][0]['review_category'], 'identity_check')

    def test_later_ambiguous_observation_preserves_previous_conflict(self):
        conflict = self.observation()
        ambiguous = self.observation('ambiguous', attempted_at='2026-11-03T11:00:00Z', checked_at='2026-11-03T11:00:00Z')
        status = self.status([conflict, ambiguous])
        self.assertEqual(len(status['findings']), 2)
        self.assertTrue(any('conflicting named business identity' in f['reason'] for f in status['findings']))
        self.assertTrue(any('incomplete or ambiguous' in f['reason'] for f in status['findings']))

    def test_newer_consistent_success_clears_prior_conflict(self):
        old = self.observation()
        fresh = self.observation('identity_and_pin_consistent', attempted_at='2026-11-03T11:00:00Z', checked_at='2026-11-03T11:00:00Z')
        status = self.status([old, fresh])
        self.assertEqual(status['counts']['current'], 1)
        self.assertFalse(status['findings'])

    def test_tampered_unknown_fields_bad_dates_and_rebound_are_discarded_or_rejected(self):
        for changes in ({'unknown': 'extra'}, {'attempted_at': 'not-a-date'},
                        {'attempted_at': '2026-11-04T10:00:00Z'}, {'checked_at': '2026-11-04T10:00:00Z'}):
            record = self.observation(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.status([record])
        rebound_point = '0' * 64
        rebound = self.observation(point_signature=rebound_point,
                                   binding_signature=digest([self.item['identity_signature'], rebound_point]))
        status = self.status([rebound])
        self.assertEqual(status['counts']['current'], 0)
        self.assertFalse(status['findings'])

    def test_bad_schema_and_url_fail_closed(self):
        with self.assertRaises(ValueError):
            self.status([self.observation(result_url='https://www.google.com/maps/search/?q=pub')])
        for url in ('https://www.google.com/maps/place/52.1,-2.2',
                    'https://google.com:443/maps/place/Named-Pub',
                    'https://user@www.google.com/maps/place/Named-Pub'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.status([self.observation(result_url=url)])
        document = dict(schema_version=1, observations=[])
        document['extra'] = True
        with self.assertRaises(ValueError):
            build_status(ROOT, document, now=NOW)


if __name__ == '__main__':
    unittest.main()
