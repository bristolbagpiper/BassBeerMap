"""Compare unresolved/review pins with exact-postcode FSA venue records."""
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from compare_pins import dist
from resolve_venue_coordinates_from_fhrs import names_match

CACHE = ROOT / "audit" / "fsa-postcode-cache"
FIELDS = [
    "venue_key", "pub_name", "place_name", "postcode", "pin_type",
    "current_lat", "current_lng", "current_source", "camra_lat", "camra_lng",
    "camra_url", "fsa_name", "fsa_address", "fsa_lat", "fsa_lng", "fsa_url",
    "current_to_fsa_metres", "camra_to_fsa_metres", "postcode_to_fsa_metres",
    "fsa_status", "checked_at",
]


def cache_path(postcode):
    digest = hashlib.sha256(postcode.encode()).hexdigest()[:20]
    return CACHE / f"{digest}.json"


def point(lat, lng):
    if lat in (None, "") or lng in (None, ""):
        return None
    return float(lat), float(lng)


def main():
    audit = list(csv.DictReader((ROOT / "audit" / "pin-audit.csv").open(encoding="utf-8")))
    postcodes = json.loads((ROOT / "pub-coordinates.json").read_text(encoding="utf-8"))["coordinates"]
    output = []
    for row in audit:
        if row["audit_status"] not in {"coordinate_disagreement", "no_unambiguous_reference", "not_yet_checked"}:
            continue
        path = cache_path(row["postcode"])
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"records": [], "url": ""}
        matches = []
        for record in document["records"]:
            geocode = record.get("geocode") or {}
            candidate = point(geocode.get("latitude"), geocode.get("longitude"))
            if candidate and names_match(row["pub_name"], record.get("BusinessName") or ""):
                matches.append((record, candidate))
        unique_points = {(round(value[1][0], 7), round(value[1][1], 7)) for value in matches}
        status = "no_name_match"
        selected = None
        if len(unique_points) == 1:
            selected = matches[0]
            status = "unique_name_and_postcode_match"
        elif len(unique_points) > 1:
            status = "ambiguous_multiple_fsa_coordinates"
        elif matches:
            status = "matching_record_without_coordinate"

        current = point(row["current_lat"], row["current_lng"])
        camra = point(row["reference_lat"], row["reference_lng"])
        postcode_data = postcodes.get(row["postcode"])
        postcode = point(postcode_data.get("lat"), postcode_data.get("lng")) if postcode_data else None
        item = {field: "" for field in FIELDS}
        item.update({field: row.get(field, "") for field in (
            "venue_key", "pub_name", "place_name", "postcode", "pin_type",
            "current_lat", "current_lng", "current_source",
        )})
        item.update(
            camra_lat=row["reference_lat"],
            camra_lng=row["reference_lng"],
            camra_url=row["reference_url"],
            fsa_url=document.get("url", ""),
            fsa_status=status,
            checked_at=document.get("checked_at", "") or row.get("checked_at", ""),
        )
        if selected:
            record, fsa = selected
            address = ", ".join(filter(None, (
                record.get("AddressLine1"), record.get("AddressLine2"),
                record.get("AddressLine3"), record.get("AddressLine4"), record.get("PostCode"),
            )))
            item.update(
                fsa_name=record.get("BusinessName", ""),
                fsa_address=address,
                fsa_lat=fsa[0],
                fsa_lng=fsa[1],
                current_to_fsa_metres=round(dist(current, fsa), 1) if current else "",
                camra_to_fsa_metres=round(dist(camra, fsa), 1) if camra else "",
                postcode_to_fsa_metres=round(dist(postcode, fsa), 1) if postcode else "",
            )
        output.append(item)

    path = ROOT / "audit" / "fsa-comparison.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(output)
    counts = {status: sum(row["fsa_status"] == status for row in output) for status in sorted({row["fsa_status"] for row in output})}
    print(f"Wrote {len(output)} rows to {path}: {counts}")


if __name__ == "__main__":
    main()
