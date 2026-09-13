import json
import tempfile
import unittest
from pathlib import Path

from build_coordinate_verification import build_records


class CoordinateVerificationTests(unittest.TestCase):
    def row(self):
        return {"pub_name": "Crown", "place_name": "Newhall", "postcode": "DE11 0XX"}

    def test_uncorroborated_coordinate_falls_back_to_postcode(self):
        key = "crown|newhall|de11 0xx"
        records = build_records(
            [self.row()], {key: {"lat": 52.0, "lng": -1.0, "source": "openstreetmap"}}, {},
            [{"venue_key": key, "audit_status": "no_unambiguous_reference"}],
            [{"venue_key": key, "fsa_status": "no_name_match", "current_to_fsa_metres": ""}],
        )
        self.assertEqual(records[key]["status"], "venue_coordinate_requires_review")
        self.assertEqual(records[key]["display_precision"], "postcode")

    def test_corroborated_coordinate_is_a_verified_venue_pin(self):
        key = "crown|newhall|de11 0xx"
        records = build_records(
            [self.row()], {key: {"lat": 52.0, "lng": -1.0, "source": "openstreetmap"}}, {},
            [{"venue_key": key, "audit_status": "reference_agrees_within_50m", "reference_url": "https://camra.example/pub", "difference_metres": "8.2"}],
            [],
        )
        self.assertEqual(records[key]["status"], "verified_venue")
        self.assertEqual(records[key]["display_precision"], "venue")
        self.assertEqual(records[key]["lat"], 52.0)
        self.assertEqual(records[key]["lng"], -1.0)

    def test_manual_override_records_postcode_conflict(self):
        key = "crown|newhall|de11 0xx"
        override = {
            key: {
                "lat": 52.0, "lng": -1.0, "source": "manual-review",
                "checked_at": "2026-09-13", "source_url": "https://example.test/place",
                "audit_reason": "manual_named_place_address_and_map_check",
                "reference_address": "1 High Street, Newhall DE11 0YY",
            }
        }
        records = build_records([self.row()], {}, override, [], [])
        self.assertEqual(records[key]["status"], "verified_venue")
        self.assertEqual(records[key]["metadata_issue"]["reference"], "DE11 0YY")


if __name__ == "__main__":
    unittest.main()
