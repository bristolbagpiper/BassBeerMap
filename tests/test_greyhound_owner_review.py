"""Exercise the owner's Greyhound correction through the scheduled job/import."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audit_map_health import health_audit
from directory_release import active_evidence, read_json, read_rows, reconcile
from owner_location_review import current_confirmation, pending_building_review


ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = 'v-c31ee814-63f4-513d-8d38-fad12edad41f'
POINT = {'lat': 52.767456, 'lng': -1.5125223}


class GreyhoundOwnerCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = read_rows(ROOT / 'pubs.csv')
        self.row = next(row for row in self.rows if row['venue_id'] == IDENTIFIER)
        self.registry = read_json(ROOT / 'venue-registry.json')

    def test_scheduled_job_retains_owner_pin_without_reopening_resolved_comparison(self):
        with tempfile.TemporaryDirectory() as output:
            with patch('audit_map_health.select_batch', return_value=[self.row]), patch('audit_map_health.time.sleep'):
                report = health_audit(ROOT, output, limit=1, check_live=False)
            self.assertTrue(report['valid'], report['errors'])
            pin = read_json(Path(output) / 'venue-registry.json')['venues'][IDENTIFIER]['verified_pin']
            self.assertEqual(POINT, {key: pin[key] for key in POINT})
            # A later reviewed PDF locality alias may change the active listing.
            # The receipt remains bound to the exact listing the owner reviewed.
            self.assertIsNotNone(current_confirmation(pin['owner_confirmation']['listing'], pin))
            self.assertIsNone(pending_building_review(self.row, pin))
            self.assertFalse(read_json(Path(output) / 'location-review.json')['records'][IDENTIFIER]['active'])
            self.assertEqual(['google-maps-named-place'], [item['source'] for item in active_evidence(pin)])
            self.assertTrue(any(item['source'] == 'camra' and 'disposition' in item for item in pin['evidence']))
            self.assertEqual('manual_review', report['checked'][0]['status'])

    def test_reviewed_pdf_locality_alias_preserves_id_and_google_point(self):
        candidate = copy.deepcopy(self.rows)
        next(row for row in candidate if row['venue_id'] == IDENTIFIER)['place_name'] = 'Boundary / Woodville'
        rows, registry, report = reconcile(candidate, self.registry, read_json(ROOT / 'ingestion-decisions.json'))
        self.assertEqual([], report['errors'])
        updated = next(row for row in rows if row['venue_id'] == IDENTIFIER)
        self.assertEqual('Boundary / Woodville', updated['place_name'])
        pin = registry['venues'][IDENTIFIER]['verified_pin']
        self.assertEqual(POINT, {key: pin[key] for key in POINT})
        self.assertEqual({row['venue_id'] for row in self.rows}, {row['venue_id'] for row in rows})
