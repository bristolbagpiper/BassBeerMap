import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from convert_pdf_to_csv import apply_postcode_overrides
from build_change_report import build_report
from send_change_report_email import format_report
from fetch_latest_pdf import download_latest_pdf
from resolve_venue_coordinates import normalise, select_backfill_candidates
from resolve_venue_coordinates_from_osm import choose_match, listing_name_variants, read_osm_venues
from build_coordinate_review_queue import build_queue
from resolve_venue_coordinates_from_fhrs import choose_match as choose_fhrs_match, names_match as fhrs_names_match
from validate_csv import validate


def row(**changes):
    value = {
        "country": "England",
        "area": "Somerset",
        "pub_name": "Example Inn",
        "place_name": "Bristol",
        "postcode": "BS1 1AA",
        "pg": "Perm",
        "last": "2026 Q3",
        "dispense": "H",
        "notes": "",
    }
    value.update(changes)
    return value


class ValidationTests(unittest.TestCase):
    def test_valid_rows_pass(self):
        valid, errors, _ = validate([row()], [row()])
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_invalid_semantic_fields_are_rejected_without_crashing(self):
        candidate = row(postcode="NOT A POSTCODE", pg="Unknown", last="September 2026", dispense="X")
        valid, errors, _ = validate([row()], [candidate])
        self.assertFalse(valid)
        self.assertTrue(any("invalid postcode" in error for error in errors))
        self.assertTrue(any("last-verified" in error for error in errors))

    def test_postcode_corrections_are_applied(self):
        rows = [row(postcode="DE4 4RF")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overrides.json"
            path.write_text(json.dumps({"DE4 4RF": {"postcode": "DE4 4FR"}}), encoding="utf-8")
            apply_postcode_overrides(rows, path)
        self.assertEqual(rows[0]["postcode"], "DE4 4FR")

    def test_change_report_lists_additions_removals_and_field_updates(self):
        previous = [row(), row(pub_name="Old Inn", postcode="BS1 1AB")]
        candidate = [row(pg="Guest"), row(pub_name="New Inn", postcode="BS1 1AC")]

        report = build_report(previous, candidate)

        self.assertTrue(report["summary"]["has_changes"])
        self.assertEqual(report["summary"]["added_count"], 1)
        self.assertEqual(report["summary"]["removed_count"], 1)
        self.assertEqual(report["summary"]["updated_count"], 1)
        self.assertEqual(report["updated"][0]["changes"]["pg"], {"previous": "Perm", "candidate": "Guest"})

    def test_free_resolver_normalises_common_pub_name_variants(self):
        self.assertEqual(normalise("The Crown and Anchor"), normalise("Crown & Anchor"))

    def test_legacy_backfill_resumes_after_the_last_processed_listing(self):
        rows = [
            row(pub_name="First Inn", postcode="BS1 1AA"),
            row(pub_name="Second Inn", postcode="BS1 1AB"),
            row(pub_name="Third Inn", postcode="BS1 1AC"),
        ]
        existing = {"first inn|bristol|bs1 1aa": {"lat": 51.45, "lng": -2.59}}

        selected, next_cursor = select_backfill_candidates(rows, existing, cursor=0, limit=2)

        self.assertEqual([item[1]["pub_name"] for item in selected], ["Second Inn", "Third Inn"])
        self.assertEqual(next_cursor, 0)

    def test_offline_osm_match_requires_local_evidence_for_duplicate_names(self):
        postcode_coordinates = {"BS1 1AA": {"lat": 51.45, "lng": -2.59}}
        candidates = [
            {"name": "Crown", "lat": 51.451, "lng": -2.591, "postcode": "", "place": ""},
            {"name": "Crown", "lat": 53.0, "lng": -1.0, "postcode": "", "place": ""},
        ]
        match = choose_match(row(pub_name="Crown"), candidates, postcode_coordinates)
        self.assertEqual(match["source"], "openstreetmap-gb-extract")
        self.assertAlmostEqual(match["lat"], 51.451)

    def test_offline_osm_reader_uses_the_centroid_of_a_mapped_pub_building(self):
        xml = """<osm version=\"0.6\"><node id=\"1\" lat=\"51.0\" lon=\"-2.0\"/><node id=\"2\" lat=\"51.002\" lon=\"-2.002\"/><way id=\"3\"><nd ref=\"1\"/><nd ref=\"2\"/><tag k=\"amenity\" v=\"pub\"/><tag k=\"name\" v=\"Outline Inn\"/></way></osm>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mini.osm"
            path.write_text(xml, encoding="utf-8")
            venues = read_osm_venues(path)
        self.assertEqual(venues[0]["name"], "Outline Inn")
        self.assertAlmostEqual(venues[0]["lat"], 51.001)
        self.assertAlmostEqual(venues[0]["lng"], -2.001)

    def test_offline_osm_variants_remove_directory_pmc_suffix(self):
        self.assertIn("Barton Rovers Social Club", listing_name_variants("Barton Rovers Social (PMC)"))

    def test_coordinate_review_queue_excludes_automated_and_manual_coordinates(self):
        rows = [row(pub_name="Mapped Inn"), row(pub_name="Reviewed Inn", postcode="BS1 1AB"), row(pub_name="Needs Review", postcode="BS1 1AC")]
        venues = {"mapped inn|bristol|bs1 1aa": {"lat": 51.45, "lng": -2.59}}
        overrides = {"reviewed inn|bristol|bs1 1ab": {"lat": 51.46, "lng": -2.58}}

        queue = build_queue(rows, venues, overrides)

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["venue_key"], "needs review|bristol|bs1 1ac")
        self.assertIn("Needs+Review", queue[0]["openstreetmap_search"])

    def test_fhrs_match_requires_same_postcode_and_one_coordinate(self):
        candidate = {
            "BusinessName": "Farmers Arms", "PostCode": "CW12 1JY",
            "geocode": {"latitude": "53.1646445", "longitude": "-2.2203405"}, "FHRSID": 1874361,
        }
        match = choose_fhrs_match(row(pub_name="Farmers", postcode="CW12 1JY"), [candidate])
        self.assertEqual(match["source"], "food-standards-agency-fhrs")
        self.assertEqual(match["fhrs_id"], 1874361)
        self.assertTrue(fhrs_names_match("The Rad (was St Radegund)", "The Rad"))

    def test_no_change_report_is_still_suitable_for_a_manual_update_email(self):
        report = build_report([row()], [row()])
        self.assertFalse(report["summary"]["has_changes"])
        self.assertIn("Added: 0 | Removed: 0 | Updated: 0", format_report(report))

    def test_pdf_download_replaces_existing_file_only_after_a_valid_download(self):
        home_page = '<h2><a href="https://example.test/latest">Latest</a></h2>'
        post_page = '<a href="https://example.test/directory.pdf">Download</a>'
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "latest-bass-directory.pdf"
            destination.write_bytes(b"old PDF")
            with patch("fetch_latest_pdf.fetch_text", side_effect=[home_page, post_page]), patch(
                "fetch_latest_pdf.fetch_bytes", return_value=b"%PDF-new"
            ):
                download_latest_pdf(destination)

            self.assertEqual(destination.read_bytes(), b"%PDF-new")
            self.assertFalse((Path(directory) / ".latest-bass-directory.pdf.download").exists())


if __name__ == "__main__":
    unittest.main()
