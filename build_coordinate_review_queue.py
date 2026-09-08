"""Create an auditable worklist for venues which need a human-verified pin.

Automated matching is intentionally conservative: a wrong blue pin is worse
than a postcode-area pin.  This script lists only the remaining listings,
excludes already-reviewed overrides, and gives each entry its stable key so a
verified coordinate can be added to venue-coordinate-overrides.json.
"""
import argparse
import csv
import json
from pathlib import Path
from urllib.parse import quote_plus

from resolve_venue_coordinates import key


FIELDS = [
    "venue_key", "pub_name", "place_name", "postcode", "area", "country",
    "openstreetmap_search", "review_status",
]


def build_queue(rows, venues, overrides):
    """Return rows that still need a real venue coordinate."""
    queue = []
    resolved = set(venues)
    reviewed = set(overrides)
    for row in rows:
        venue_key = key(row)
        if venue_key in resolved or venue_key in reviewed:
            continue
        query = ", ".join(value for value in (
            row["pub_name"], row["place_name"], row["postcode"], "United Kingdom"
        ) if value)
        queue.append({
            "venue_key": venue_key,
            "pub_name": row["pub_name"],
            "place_name": row["place_name"],
            "postcode": row["postcode"],
            "area": row["area"],
            "country": row["country"],
            "openstreetmap_search": f"https://www.openstreetmap.org/search?query={quote_plus(query)}",
            "review_status": "needs_verified_venue_coordinate",
        })
    return sorted(queue, key=lambda item: (item["country"], item["area"], item["place_name"], item["pub_name"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", default="pubs.csv")
    parser.add_argument("--venues", default="venue-coordinates.json")
    parser.add_argument("--overrides", default="venue-coordinate-overrides.json")
    parser.add_argument("--output", default="coordinate-review-queue.csv")
    args = parser.parse_args()

    rows = list(csv.DictReader(Path(args.csv_path).open(encoding="utf-8", newline="")))
    venues = json.loads(Path(args.venues).read_text(encoding="utf-8")).get("venues", {})
    overrides_path = Path(args.overrides)
    overrides = json.loads(overrides_path.read_text(encoding="utf-8")).get("venues", {}) if overrides_path.exists() else {}
    queue = build_queue(rows, venues, overrides)

    with Path(args.output).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(queue)
    print(f"{len(queue):,} listings need a human-verified venue coordinate.")


if __name__ == "__main__":
    main()
