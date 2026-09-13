"""Build the production coordinate-verification ledger.

Only venue coordinates corroborated by the audit data are allowed to render as
blue pins. Everything else deliberately falls back to its postcode point.
"""
import argparse
import csv
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VERIFIED_AUDIT_STATUSES = {
    "reference_agrees_within_50m",
    "reference_agrees_within_50m_postcode_conflict",
}
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)


def venue_key(row):
    return "|".join(row[field].strip().lower() for field in ("pub_name", "place_name", "postcode"))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_records(rows, venue_coordinates, overrides, audit_rows, fsa_rows):
    audit = {row["venue_key"]: row for row in audit_rows}
    fsa = {row["venue_key"]: row for row in fsa_rows}
    records = {}

    for row in rows:
        key = venue_key(row)
        coordinate = overrides.get(key) or venue_coordinates.get(key)
        audit_row = audit.get(key, {})
        fsa_row = fsa.get(key, {})
        evidence = []
        status = "postcode_approximate"
        display_precision = "postcode"

        if coordinate:
            camra_agrees = audit_row.get("audit_status") in VERIFIED_AUDIT_STATUSES
            fsa_distance = fsa_row.get("current_to_fsa_metres", "")
            fsa_agrees = (
                fsa_row.get("fsa_status") == "unique_name_and_postcode_match"
                and fsa_distance not in (None, "")
                and float(fsa_distance) <= 50
            )
            audited_override = bool(
                key in overrides
                and overrides[key].get("checked_at")
                and overrides[key].get("source_url")
                and overrides[key].get("audit_reason")
            )

            if camra_agrees:
                evidence.append({
                    "source": "camra",
                    "url": audit_row.get("reference_url", ""),
                    "difference_metres": float(audit_row.get("difference_metres") or 0),
                    "match": "venue_name_and_full_postcode",
                    "checked_at": audit_row.get("checked_at", ""),
                })
                if audit_row.get("audit_status") == "reference_agrees_within_50m_postcode_conflict":
                    evidence[-1]["match"] = "venue_name_and_coordinate; listing_postcode_conflicts"
                    evidence[-1]["reference_postcode"] = audit_row.get("reference_postcode", "")
            if fsa_agrees:
                evidence.append({
                    "source": "food_standards_agency",
                    "url": fsa_row.get("fsa_url", ""),
                    "difference_metres": float(fsa_distance),
                    "match": "venue_name_or_safe_alias_and_full_postcode",
                    "checked_at": fsa_row.get("checked_at", ""),
                })
            if audited_override:
                evidence.append({
                    "source": overrides[key].get("source", "manual_override"),
                    "url": overrides[key]["source_url"],
                    "difference_metres": 0,
                    "match": overrides[key]["audit_reason"],
                    "checked_at": overrides[key]["checked_at"],
                })

            if evidence:
                status = "verified_venue"
                display_precision = "venue"
            else:
                status = "venue_coordinate_requires_review"

        records[key] = {
            "pub_name": row["pub_name"],
            "place_name": row["place_name"],
            "postcode": row["postcode"],
            "status": status,
            "display_precision": display_precision,
            "coordinate_source": coordinate.get("source", "") if coordinate else "postcode",
            "evidence": evidence,
        }
        if coordinate:
            records[key]["lat"] = float(coordinate["lat"])
            records[key]["lng"] = float(coordinate["lng"])
        if audit_row.get("audit_status") == "reference_agrees_within_50m_postcode_conflict":
            records[key]["metadata_issue"] = {
                "field": "postcode",
                "listed": row["postcode"],
                "reference": audit_row.get("reference_postcode", ""),
                "source_url": audit_row.get("reference_url", ""),
            }
        elif key in overrides and overrides[key].get("reference_address"):
            match = POSTCODE_RE.search(overrides[key]["reference_address"])
            reference_postcode = match.group(1).upper() if match else ""
            if reference_postcode and reference_postcode.replace(" ", "") != row["postcode"].replace(" ", ""):
                records[key]["metadata_issue"] = {
                    "field": "postcode",
                    "listed": row["postcode"],
                    "reference": reference_postcode,
                    "source_url": overrides[key].get("source_url", ""),
                }

    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root
    rows = list(csv.DictReader((root / "pubs.csv").open(encoding="utf-8", newline="")))
    venues = load_json(root / "venue-coordinates.json").get("venues", {})
    overrides = load_json(root / "venue-coordinate-overrides.json").get("venues", {})
    audit_rows = list(csv.DictReader((root / "audit/pin-audit.csv").open(encoding="utf-8", newline="")))
    fsa_rows = list(csv.DictReader((root / "audit/fsa-comparison.csv").open(encoding="utf-8", newline="")))
    records = build_records(rows, venues, overrides, audit_rows, fsa_rows)
    counts = {}
    for record in records.values():
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    evidence_dates = [
        item.get("checked_at", "")
        for record in records.values()
        for item in record.get("evidence", [])
        if item.get("checked_at")
    ]
    payload = {
        "schema_version": 1,
        "generated_at": max(evidence_dates),
        "policy": {
            "venue_pin": "A named venue coordinate corroborated within 50 metres by CAMRA/FSA, or an evidence-backed manual override.",
            "postcode_pin": "The postcode centroid; approximate and never presented as a verified venue location.",
            "failure_mode": "Fail closed to the postcode pin when verification is absent or cannot be loaded.",
        },
        "counts": counts,
        "records": records,
    }
    output = root / "coordinate-verification.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(records):,} records to {output}: {counts}")


if __name__ == "__main__":
    main()
