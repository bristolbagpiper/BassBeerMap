import json
import tempfile
import unittest
from pathlib import Path

from convert_pdf_to_csv import apply_postcode_overrides
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


if __name__ == "__main__":
    unittest.main()
