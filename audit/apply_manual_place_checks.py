#!/usr/bin/env python3
"""Apply individually reviewed named-place coordinates as traceable overrides."""
import csv
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def distance_metres(lat1, lng1, lat2, lng2):
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def main():
    overrides_path = ROOT / "venue-coordinate-overrides.json"
    venues_path = ROOT / "venue-coordinates.json"
    checks_path = ROOT / "audit/manual-place-checks.csv"
    overrides_payload = json.loads(overrides_path.read_text(encoding="utf-8"))
    overrides = overrides_payload.setdefault("venues", {})
    generated = json.loads(venues_path.read_text(encoding="utf-8")).get("venues", {})

    rows = list(csv.DictReader(checks_path.open(encoding="utf-8", newline="")))
    for row in rows:
        key = row["venue_key"]
        previous = overrides.get(key) or generated.get(key)
        lat, lng = float(row["lat"]), float(row["lng"])
        record = {
            "lat": lat,
            "lng": lng,
            "source": row["source"],
            "source_url": row["source_url"],
            "checked_at": row["checked_at"],
            "audit_reason": "manual_named_place_address_and_map_check",
            "reference_name": row["reference_name"],
            "reference_address": row["reference_address"],
            "review_note": row["review_note"],
        }
        if row["corroborating_source_url"]:
            record["corroborating_source_url"] = row["corroborating_source_url"]
        if previous and previous.get("audit_reason") == "manual_named_place_address_and_map_check":
            for field in ("previous_lat", "previous_lng", "previous_source", "previous_difference_metres"):
                if field in previous:
                    record[field] = previous[field]
        elif previous:
            record.update({
                "previous_lat": previous["lat"],
                "previous_lng": previous["lng"],
                "previous_source": previous.get("source", "venue"),
                "previous_difference_metres": round(
                    distance_metres(float(previous["lat"]), float(previous["lng"]), lat, lng), 1
                ),
            })
        else:
            record["previous_source"] = "postcode"
        overrides[key] = record

    overrides_payload["venues"] = dict(sorted(overrides.items()))
    overrides_path.write_text(
        json.dumps(overrides_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Applied {len(rows)} manual checks; {len(overrides)} overrides total")


if __name__ == "__main__":
    main()
