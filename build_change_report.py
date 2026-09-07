"""Create an auditable listing-by-listing report for a directory refresh."""
import argparse
import csv
import json
from pathlib import Path


DISPLAY_FIELDS = ("country", "area", "pub_name", "place_name", "postcode", "pg", "last", "dispense", "notes")
COMPARE_FIELDS = ("country", "area", "pg", "last", "dispense", "notes")


def read_rows(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def listing_key(row):
    return "|".join(str(row.get(field, "")).strip().casefold() for field in ("pub_name", "place_name", "postcode"))


def public_row(row):
    return {field: str(row.get(field, "")).strip() for field in DISPLAY_FIELDS}


def build_report(previous_rows, candidate_rows):
    previous = {listing_key(row): row for row in previous_rows}
    candidate = {listing_key(row): row for row in candidate_rows}
    added = [public_row(candidate[key]) for key in sorted(candidate.keys() - previous.keys())]
    removed = [public_row(previous[key]) for key in sorted(previous.keys() - candidate.keys())]
    updated = []
    for key in sorted(previous.keys() & candidate.keys()):
        changes = {
            field: {"previous": str(previous[key].get(field, "")).strip(), "candidate": str(candidate[key].get(field, "")).strip()}
            for field in COMPARE_FIELDS
            if str(previous[key].get(field, "")).strip() != str(candidate[key].get(field, "")).strip()
        }
        if changes:
            updated.append({"listing": public_row(candidate[key]), "changes": changes})
    return {
        "summary": {
            "previous_listing_count": len(previous_rows),
            "candidate_listing_count": len(candidate_rows),
            "added_count": len(added),
            "removed_count": len(removed),
            "updated_count": len(updated),
            "has_changes": bool(added or removed or updated),
        },
        "added": added,
        "removed": removed,
        "updated": updated,
    }


def main():
    parser = argparse.ArgumentParser(description="Compare the current and candidate Bass listings.")
    parser.add_argument("candidate")
    parser.add_argument("--previous", default="pubs.csv")
    parser.add_argument("--output", default="change-report.json")
    args = parser.parse_args()
    report = build_report(read_rows(args.previous), read_rows(args.candidate))
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
