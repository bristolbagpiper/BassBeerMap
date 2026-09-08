import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from convert_pdf_to_csv import apply_postcode_overrides
from build_change_report import build_report
from ai_venue_resolver import listing_key as venue_listing_key
from fetch_latest_pdf import download_latest_pdf
from resolve_venue_coordinates import normalise
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

    def test_ai_resolver_uses_the_same_stable_venue_identity(self):
        self.assertEqual(venue_listing_key(row(pub_name="The New Inn", place_name="Town", postcode="AB1 2CD")), "the new inn|town|ab1 2cd")

    def test_free_resolver_normalises_common_pub_name_variants(self):
        self.assertEqual(normalise("The Crown and Anchor"), normalise("Crown & Anchor"))

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
