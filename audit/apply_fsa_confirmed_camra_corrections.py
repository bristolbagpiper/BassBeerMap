"""Apply CAMRA corrections independently corroborated by an FSA point.

Eligible records have a unique venue-name match at the exact postcode, the
FSA and CAMRA points agree within 50 metres, and the existing point is more
than 50 metres from both.  Existing audit history is retained in each override.
"""
import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_PATH = ROOT / "venue-coordinate-overrides.json"


def main():
    audit = {
        row["venue_key"]: row
        for row in csv.DictReader((ROOT / "audit" / "pin-audit.csv").open(encoding="utf-8"))
    }
    with (ROOT / "audit" / "fsa-comparison.csv").open(encoding="utf-8", newline="") as handle:
        comparisons = list(csv.DictReader(handle))
    document = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    overrides = document.setdefault("venues", {})
    applied = []

    for row in comparisons:
        if row["fsa_status"] != "unique_name_and_postcode_match" or not row["camra_lat"]:
            continue
        if not (
            float(row["camra_to_fsa_metres"]) <= 50
            and float(row["current_to_fsa_metres"]) > 50
        ):
            continue
        previous = audit[row["venue_key"]]
        overrides[row["venue_key"]] = {
            "lat": float(row["camra_lat"]),
            "lng": float(row["camra_lng"]),
            "source": "camra-and-fsa-location-audit",
            "source_url": row["camra_url"],
            "corroborating_source_url": row["fsa_url"],
            "checked_at": "2026-09-11",
            "audit_reason": "camra_and_fsa_agree_within_50m_current_pin_does_not",
            "previous_lat": float(previous["current_lat"]),
            "previous_lng": float(previous["current_lng"]),
            "previous_source": previous["current_source"],
            "previous_difference_metres": float(previous["difference_metres"]),
            "camra_to_fsa_metres": float(row["camra_to_fsa_metres"]),
        }
        applied.append(row["venue_key"])

    OVERRIDES_PATH.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Applied {len(applied)} two-source coordinate corrections")


if __name__ == "__main__":
    main()
