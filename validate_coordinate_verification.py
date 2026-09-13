"""Fail CI when the map could show an unverified coordinate as a blue pin."""
import argparse
import csv
import json
import math
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ALLOWED_STATUSES = {"verified_venue", "venue_coordinate_requires_review", "postcode_approximate"}


def venue_key(row):
    return "|".join(row[field].strip().lower() for field in ("pub_name", "place_name", "postcode"))


def distance_metres(left, right):
    lat1, lon1, lat2, lon2 = map(math.radians, (*left, *right))
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))


def validate(root=ROOT, require_all_verified=False, max_evidence_age_days=None):
    rows = list(csv.DictReader((root / "pubs.csv").open(encoding="utf-8", newline="")))
    venues = json.loads((root / "venue-coordinates.json").read_text(encoding="utf-8")).get("venues", {})
    overrides = json.loads((root / "venue-coordinate-overrides.json").read_text(encoding="utf-8")).get("venues", {})
    postcodes = json.loads((root / "pub-coordinates.json").read_text(encoding="utf-8")).get("coordinates", {})
    ledger = json.loads((root / "coordinate-verification.json").read_text(encoding="utf-8"))
    records = ledger.get("records", {})
    errors = []
    keys = [venue_key(row) for row in rows]

    if len(keys) != len(set(keys)):
        errors.append("pubs.csv contains duplicate venue keys")
    if set(keys) != set(records):
        missing = sorted(set(keys) - set(records))
        stale = sorted(set(records) - set(keys))
        errors.append(f"verification coverage mismatch: {len(missing)} missing, {len(stale)} stale")

    actual_counts = {}
    for record in records.values():
        status = record.get("status")
        actual_counts[status] = actual_counts.get(status, 0) + 1
    if ledger.get("counts") != actual_counts:
        errors.append("verification summary counts do not match the ledger records")

    for row, key in zip(rows, keys):
        record = records.get(key)
        if not record:
            continue
        status = record.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{key}: invalid status {status!r}")
            continue
        if require_all_verified and status != "verified_venue":
            errors.append(f"{key}: full-audit mode requires a verified venue coordinate")
        coordinate = overrides.get(key) or venues.get(key)
        if record.get("display_precision") == "venue":
            if status != "verified_venue" or not coordinate:
                errors.append(f"{key}: blue pin is not a verified stored venue coordinate")
            if not record.get("evidence"):
                errors.append(f"{key}: blue pin has no recorded evidence")
            if coordinate and (
                float(record.get("lat", 999)) != float(coordinate["lat"])
                or float(record.get("lng", 999)) != float(coordinate["lng"])
            ):
                errors.append(f"{key}: ledger coordinate differs from the rendered coordinate")
            for item in record.get("evidence", []):
                if not item.get("source") or not item.get("url"):
                    errors.append(f"{key}: incomplete evidence record")
            dated_evidence = [item.get("checked_at") for item in record.get("evidence", []) if item.get("checked_at")]
            if not dated_evidence:
                errors.append(f"{key}: blue pin has no dated evidence")
            elif max_evidence_age_days is not None:
                newest = max(date.fromisoformat(value) for value in dated_evidence)
                if (date.today() - newest).days > max_evidence_age_days:
                    errors.append(f"{key}: newest evidence is older than {max_evidence_age_days} days")
        elif record.get("display_precision") == "postcode":
            if row["postcode"] not in postcodes:
                errors.append(f"{key}: postcode fallback has no coordinate")
        else:
            errors.append(f"{key}: invalid display precision")

    # Catch the old failure mode: different postcodes sharing one supposedly
    # exact point. Same-site duplicate listings remain possible and are
    # reviewed separately, but cross-postcode reuse is never acceptable.
    points = {}
    for row, key in zip(rows, keys):
        record = records.get(key, {})
        if record.get("display_precision") != "venue":
            continue
        coordinate = overrides.get(key) or venues.get(key)
        point = (round(float(coordinate["lat"]), 6), round(float(coordinate["lng"]), 6))
        previous = points.get(point)
        if previous and previous[0] != row["postcode"]:
            errors.append(f"{key}: exact point is reused by different postcode {previous[0]} ({previous[1]})")
        points[point] = (row["postcode"], key)

    # The manual review CSV is the reproducible source for individually checked
    # places. It must never drift from the production override file.
    manual_path = root / "audit/manual-place-checks.csv"
    if manual_path.exists():
        for row in csv.DictReader(manual_path.open(encoding="utf-8", newline="")):
            key = row["venue_key"]
            override = overrides.get(key)
            if not override:
                errors.append(f"{key}: manual place check has no production override")
                continue
            if float(row["lat"]) != float(override["lat"]) or float(row["lng"]) != float(override["lng"]):
                errors.append(f"{key}: manual check coordinate differs from production override")
            if not row.get("source_url") or not row.get("checked_at") or not row.get("review_note"):
                errors.append(f"{key}: manual place check is missing its audit trail")

    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-all-verified", action="store_true")
    parser.add_argument("--max-evidence-age-days", type=int)
    args = parser.parse_args()
    problems = validate(
        require_all_verified=args.require_all_verified,
        max_evidence_age_days=args.max_evidence_age_days,
    )
    if problems:
        print("Coordinate verification failed:")
        for problem in problems:
            print(f"- {problem}")
        raise SystemExit(1)
    print("Coordinate verification passed")
