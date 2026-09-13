"""Build a complete, severity-ordered queue from the pin audit."""
import collections
import csv
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FIELDS = [
    "priority", "issue", "row_number", "venue_key", "pub_name", "place_name",
    "postcode", "pin_type", "current_lat", "current_lng", "current_source",
    "postcode_displacement_metres", "reference_name", "reference_lat",
    "reference_lng", "difference_metres", "reference_url", "audit_status",
]


def distance_metres(left, right):
    lat1, lon1, lat2, lon2 = map(math.radians, (*left, *right))
    value = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))


def main():
    audit = list(csv.DictReader((ROOT / "audit" / "pin-audit.csv").open(encoding="utf-8")))
    ledger = json.loads((ROOT / "coordinate-verification.json").read_text(encoding="utf-8"))["records"]
    overrides = json.loads((ROOT / "venue-coordinate-overrides.json").read_text(encoding="utf-8"))["venues"]
    postcodes = json.loads((ROOT / "pub-coordinates.json").read_text(encoding="utf-8"))["coordinates"]
    key_counts = collections.Counter(row["venue_key"] for row in audit)
    queue = []

    for row in audit:
        verification = ledger.get(row["venue_key"], {})
        override = overrides.get(row["venue_key"], {})
        postcode_point = postcodes.get(row["postcode"])
        displacement = ""
        if postcode_point and row["current_lat"] and row["current_lng"]:
            displacement = round(distance_metres(
                (float(row["current_lat"]), float(row["current_lng"])),
                (postcode_point["lat"], postcode_point["lng"]),
            ), 1)

        issues = []
        if key_counts[row["venue_key"]] > 1:
            issues.append((1, "duplicate_listing_key"))
        if verification.get("metadata_issue", {}).get("field") == "postcode":
            issues.append((1, "listing_postcode_conflicts_with_venue_reference"))
        if "source-name typo" in override.get("review_note", "") or "name appears truncated" in override.get("review_note", ""):
            issues.append((1, "listing_name_requires_correction"))
        if row["audit_status"] == "not_yet_checked" and verification.get("status") != "verified_venue":
            issues.append((1, "independent_reference_not_checked"))
        elif row["audit_status"] == "postcode_pin_reference_found" and verification.get("status") != "verified_venue":
            issues.append((1, "postcode_pin_has_matching_venue"))
        elif row["audit_status"] == "coordinate_disagreement" and verification.get("status") != "verified_venue":
            delta = float(row["difference_metres"])
            issues.append((1 if delta > 250 else 2, "coordinate_disagreement"))
        elif row["audit_status"] == "no_unambiguous_reference" and verification.get("status") != "verified_venue":
            issues.append((3, "no_unambiguous_camra_reference"))

        for priority, issue in issues:
            item = {field: row.get(field, "") for field in FIELDS}
            item.update(priority=priority, issue=issue, postcode_displacement_metres=displacement)
            metadata_issue = verification.get("metadata_issue", {})
            if metadata_issue:
                item["reference_url"] = metadata_issue.get("source_url", item["reference_url"])
            queue.append(item)

    queue.sort(key=lambda row: (
        int(row["priority"]),
        -float(row["difference_metres"] or row["postcode_displacement_metres"] or 0),
        row["venue_key"],
    ))
    output = ROOT / "audit" / "anomaly-queue.csv"
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(queue)
    print(f"Wrote {len(queue)} actionable audit rows to {output}")


if __name__ == "__main__":
    main()
