import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import audit_google_places
from audit_google_comparisons import directory_binding, route
from build_place_review_queue import signatures
from directory_release import read_json, write_json
from location_review import GOOGLE_STALE_STATUS_ISSUE, with_google


REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


class GoogleComparisonsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = read_json(REPO / 'directory-release.json')
        write_json(self.root / 'directory-release.json', self.bundle)
        write_json(self.root / 'google-review-provider.json', {'provider': 'hermes-browser'})
        self.bindings = {row['venue_id']: signatures(row, self.bundle['locations'][row['venue_id']])['binding_signature']
                         for row in self.bundle['rows']}
        self.first = self.bundle['rows'][0]

    def tearDown(self):
        self.temp.cleanup()

    def finding(self, venue=None, binding=None):
        venue = venue or self.first
        return dict(venue_id=venue['venue_id'], pub_name=venue['pub_name'], place_name=venue.get('place_name', ''),
                    postcode=venue.get('postcode', ''), status='review_needed', reason='Google reports a conflicting named business identity',
                    binding_signature=binding or self.bindings[venue['venue_id']])

    def source_status(self, checked_at='2026-11-03T10:00:00Z', **changes):
        total = len(self.bundle['rows'])
        record = dict(schema_version=1, provider='hermes-browser', checked_at=checked_at, configured=True,
                      directory_binding_signature=directory_binding(self.bundle), issues=[],
                      findings=[self.finding()],
                      counts=dict(total=total, current=1, unchecked_or_stale=total-1,
                                  checked_this_run=1, attempted_venue_count=1, review_count=1),
                      hard_failure=False)
        record.update(changes)
        write_json(self.root / 'hermes-google-status.json', record)
        return record

    def run_route(self):
        return route(self.root, self.root / 'state.json', self.root / 'out.json', limit=45,
                     enabled=True, api_key='present-but-never-used', now=NOW)

    def test_missing_and_empty_status_are_coverage_gaps_not_api_calls(self):
        with patch.object(audit_google_places, 'audit', side_effect=AssertionError('API provider invoked')) as api:
            status, code = self.run_route()
        api.assert_not_called()
        self.assertEqual(code, 0)
        self.assertTrue(status['configured'])
        self.assertEqual(status['counts']['current'], 0)
        self.assertEqual(status['counts']['unchecked_or_stale'], status['counts']['total'])
        self.assertTrue(any('results are missing' in issue for issue in status['issues']))

    def test_existing_source_checked_at_is_preserved_and_staleness_is_visible(self):
        self.source_status(checked_at='2026-10-01T10:00:00Z')
        status, code = self.run_route()
        self.assertEqual(code, 0)
        self.assertEqual(status['checked_at'], '2026-10-01T10:00:00Z')
        attached = with_google(dict(records={}), status, self.bundle, now=NOW)
        self.assertIn(GOOGLE_STALE_STATUS_ISSUE, attached['google_issues'])
        self.assertEqual(status['findings'], [self.finding()])

    def test_invalid_status_writes_safe_hard_failure_and_exits_one(self):
        write_json(self.root / 'hermes-google-status.json', {'provider': 'hermes-browser', 'checked_at': 'bad'})
        status, code = self.run_route()
        self.assertEqual(code, 1)
        self.assertTrue(status['hard_failure'])
        self.assertEqual(status['findings'], [])
        self.assertNotIn('bad', json.dumps(status))
        self.assertTrue(status['issues'])

    def test_moved_pin_resets_coverage_and_drops_rebound_findings(self):
        source = self.source_status()
        changed = copy.deepcopy(self.bundle)
        changed['locations'][self.first['venue_id']]['lat'] += 0.01
        write_json(self.root / 'directory-release.json', changed)
        status, code = self.run_route()
        self.assertEqual(code, 0)
        self.assertEqual(status['counts']['current'], 0)
        self.assertEqual(status['counts']['unchecked_or_stale'], len(changed['rows']))
        self.assertEqual(status['findings'], [])
        self.assertNotEqual(source['directory_binding_signature'], status['directory_binding_signature'])

    def test_unknown_configuration_keeps_api_default_for_back_compatibility(self):
        (self.root / 'google-review-provider.json').unlink()
        expected = dict(schema_version=1, configured=False, checked_at=NOW.isoformat(), issues=[],
                        counts={}, findings=[], hard_failure=False)
        with patch.object(audit_google_places, 'audit', return_value=expected) as api:
            status, code = route(self.root, self.root / 'state.json', self.root / 'out.json', enabled=True,
                                 api_key='key', now=NOW)
        api.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(status['provider'], 'places-api')


if __name__ == '__main__':
    unittest.main()
