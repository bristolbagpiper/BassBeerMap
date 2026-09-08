"""Resolve directory venues against the UK Food Standards Agency FHRS API.

This is a free, official source of food-business names, addresses and geocoded
locations. It is deliberately used only after the offline OSM match, and only
accepts an unambiguous result with the directory's exact postcode.
"""
import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from resolve_venue_coordinates import key, name_variants, normalise

API_URL = "https://api.ratings.food.gov.uk/Establishments"
USER_AGENT = "BassBeerMap coordinate resolver (github.com/bristolbagpiper/BassBeerMap)"
GENERIC_SUFFIXES = {"arms", "inn", "hotel", "tavern", "pub", "bar", "club", "lounge"}


def listing_name_variants(name):
    variants = name_variants(name)
    stripped = re.sub(r"\s*\((?:PMC|was\s+[^)]*)\)\s*", "", name, flags=re.IGNORECASE).strip()
    if stripped:
        variants.extend(name_variants(stripped))
    return list(dict.fromkeys(variants))


def names_match(listing_name, business_name):
    """Accept exact names and harmless omitted pub-type suffixes only."""
    candidate = normalise(business_name)
    for variant in listing_name_variants(listing_name):
        wanted = normalise(variant)
        if wanted == candidate:
            return True
        for suffix in GENERIC_SUFFIXES:
            if candidate == wanted + suffix or wanted == candidate + suffix:
                return True
    return False


def search(row):
    params = urlencode({
        "name": re.sub(r"\s*\([^)]*\)", "", row["pub_name"]).strip(),
        "address": row["postcode"],
        "pageNumber": 1,
        "pageSize": 20,
    })
    request = Request(API_URL + "?" + params, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "x-api-version": "2",
    })
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read()).get("establishments", [])


def choose_match(row, candidates):
    expected_postcode = re.sub(r"\s+", "", row["postcode"]).upper()
    matches = []
    for candidate in candidates:
        postcode = re.sub(r"\s+", "", str(candidate.get("PostCode") or "")).upper()
        geocode = candidate.get("geocode") or {}
        try:
            lat = float(geocode["latitude"])
            lng = float(geocode["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if postcode != expected_postcode or not names_match(row["pub_name"], str(candidate.get("BusinessName") or "")):
            continue
        matches.append((lat, lng, candidate))
    # More than one FSA record can exist for a venue after a transfer of owner.
    # Accept it only when every matching record has the same geocoded point.
    points = {(lat, lng) for lat, lng, _ in matches}
    if len(points) != 1:
        return None
    lat, lng = next(iter(points))
    candidate = matches[0][2]
    return {
        "lat": lat,
        "lng": lng,
        "source": "food-standards-agency-fhrs",
        "fhrs_id": candidate.get("FHRSID"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", default="pubs.csv")
    parser.add_argument("output", nargs="?", default="venue-coordinates.json")
    parser.add_argument("--existing", default="venue-coordinates.json")
    parser.add_argument("--max-venues", type=int, default=0, help="Limit unresolved lookups; 0 resolves all.")
    args = parser.parse_args()

    rows = list(csv.DictReader(Path(args.csv_path).open(encoding="utf-8", newline="")))
    existing = json.loads(Path(args.existing).read_text(encoding="utf-8"))
    listing_keys = {key(row) for row in rows}
    venues = {venue_key: value for venue_key, value in existing.get("venues", {}).items() if venue_key in listing_keys}
    pending = [row for row in rows if key(row) not in venues]
    if args.max_venues:
        pending = pending[:args.max_venues]

    matched = 0
    for index, row in enumerate(pending, start=1):
        try:
            match = choose_match(row, search(row))
        except Exception as error:
            print(f"{index}/{len(pending)}: {row['pub_name']} lookup failed: {error}")
            continue
        if match:
            venues[key(row)] = match
            matched += 1
            print(f"{index}/{len(pending)}: {row['pub_name']} matched")
        else:
            print(f"{index}/{len(pending)}: {row['pub_name']} not matched")

    unresolved = sorted(key(row) for row in rows if key(row) not in venues)
    Path(args.output).write_text(json.dumps({
        "venues": venues,
        "unresolved_venues": unresolved,
        "backfill_cursor": 0,
    }, separators=(",", ":")), encoding="utf-8")
    print(f"FHRS matched {matched:,}; wrote {len(venues):,} precise venue coordinates; {len(unresolved):,} need review.")


if __name__ == "__main__":
    main()
