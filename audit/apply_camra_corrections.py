"""Apply only high-confidence corrections found by the CAMRA pin audit.

The comparison has already required an exact postcode and a normalised venue
name match.  This script promotes:

* postcode fallback pins with one matching CAMRA venue; and
* existing venue pins that are more than 50 metres from that venue.

Differences up to 50 metres are treated as normal entrance/building-centroid
variation. Above that threshold the unique CAMRA name-and-full-postcode venue
point wins over a generic geocoder or automated OpenStreetMap match. The script
is deterministic and preserves every unrelated existing override.
"""
import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = ROOT / "audit" / "pin-audit.csv"
OVERRIDES_PATH = ROOT / "venue-coordinate-overrides.json"
CHECKED_AT = "2026-09-11"


def correction_reason(row):
    if row["audit_status"] == "postcode_pin_reference_found":
        return "postcode_fallback_replaced"
    if (
        row["audit_status"] == "coordinate_disagreement"
        and float(row["difference_metres"]) > 50
    ):
        return "existing_pin_over_50m_from_matching_venue"
    return None


def main():
    document = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    overrides = document.setdefault("venues", {})
    applied = []

    with AUDIT_PATH.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            reason = correction_reason(row)
            if not reason:
                continue
            overrides[row["venue_key"]] = {
                "lat": float(row["reference_lat"]),
                "lng": float(row["reference_lng"]),
                "source": "camra-pubs-location-audit",
                "source_url": row["reference_url"],
                "checked_at": CHECKED_AT,
                "audit_reason": reason,
                "previous_lat": float(row["current_lat"]),
                "previous_lng": float(row["current_lng"]),
                "previous_source": row["current_source"],
                "previous_difference_metres": float(row["difference_metres"]),
            }
            applied.append((row["venue_key"], reason))

    OVERRIDES_PATH.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reasons = {reason: sum(item[1] == reason for item in applied) for _, reason in applied}
    print(f"Applied {len(applied)} CAMRA-backed coordinate overrides: {reasons}")


if __name__ == "__main__":
    main()
