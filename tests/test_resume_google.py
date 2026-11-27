import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import resume_google
from directory_release import write_json, write_rows


class ResumeGoogleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_root = resume_google.ROOT
        resume_google.ROOT = self.root
        self.row = dict(country='England', area='Somerset', pub_name='Example Inn', place_name='Bristol',
                        postcode='BS1 1AA', pg='Perm', last='2026 Q3', dispense='H', notes='', venue_id='venue-one')
        write_rows(self.root / 'pubs.csv', [self.row])
        self.registry = {'schema_version': 1, 'venues': {'venue-one': {
            'aliases': ['example'], 'current_key': 'example', 'listing': dict(self.row),
            'verified_pin': {'lat': 51.45, 'lng': -2.59}, 'google_priority_pending': True}},
            'location_review': {'schema_version': 1, 'records': {}}}
        write_json(self.root / 'venue-registry.json', self.registry)
        write_json(self.root / 'directory-meta.json', {})
        write_json(self.root / 'pub-coordinates.json', {'coordinates': {'BS1 1AA': {'lat': 51.45, 'lng': -2.59}}})
        write_json(self.root / 'coordinate-verification.json', {'records': {}})
        write_json(self.root / 'trading-notices.json', {'schema_version': 1, 'notices': [], 'manual_hides': []})
        write_json(self.root / 'directory-release.json', {'release_id': 'old'})
        write_json(self.root / 'location-review.json', self.registry['location_review'])

    def tearDown(self):
        resume_google.ROOT = self.original_root
        self.temp.cleanup()

    @patch('resume_google.build_bundle', return_value={'release_id': 'new'})
    @patch('location_review.build_review', return_value={'schema_version': 1, 'records': {'venue-one': {'active': True}}})
    @patch('google_listings.check_google')
    def test_persists_only_google_resume_progress_and_derived_review(self, check, _review, _bundle):
        def checked(rows, registry, previous, pending_only=False):
            self.assertEqual(rows[0]['venue_id'], 'venue-one')
            self.assertTrue(pending_only)
            registry['venues']['venue-one']['google_check'] = {'outcome': 'ambiguous'}
            registry['venues']['venue-one'].pop('google_priority_pending', None)
            return {'venue-one': 'ambiguous'}
        check.side_effect = checked

        resume_google.main()

        registry = json.loads((self.root / 'venue-registry.json').read_text(encoding='utf-8'))
        self.assertEqual(registry['venues']['venue-one']['google_check']['outcome'], 'ambiguous')
        self.assertEqual(registry['location_review']['records']['venue-one']['active'], True)
        self.assertEqual(json.loads((self.root / 'directory-release.json').read_text())['release_id'], 'new')
        self.assertEqual(json.loads((self.root / 'location-review.json').read_text()), registry['location_review'])

    @patch('resume_google.build_bundle')
    @patch('location_review.build_review')
    @patch('google_listings.check_google', return_value={'browser': 'unavailable: WebDriverException'})
    def test_unavailable_without_durable_progress_writes_nothing(self, check, review, bundle):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        resume_google.main()
        after = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(after, before)
        review.assert_not_called()
        bundle.assert_not_called()
        check.assert_called_once_with([self.row], unittest.mock.ANY, [self.row], pending_only=True)


if __name__ == '__main__':
    unittest.main()
