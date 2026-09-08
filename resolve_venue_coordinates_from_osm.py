"""Match Bass directory listings to venue objects in an OpenStreetMap PBF extract.

This is deliberately an offline resolver: it never bulk-queries a public
geocoding API.  The caller supplies a downloaded Great Britain OSM extract.
"""
import argparse
import csv
import json
import math
import re
from pathlib import Path

from resolve_venue_coordinates import key, name_variants, normalise

VENUE_AMENITIES = {"pub", "bar", "social_club", "club"}


def compact_postcode(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def distance_km(left, right):
    """Great-circle distance, sufficient for choosing among same-name venues."""
    lat1, lon1, lat2, lon2 = map(math.radians, (left["lat"], left["lng"], right["lat"], right["lng"]))
    return 6371 * 2 * math.asin(
        math.sqrt(math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    )


def read_osm_venues(path):
    """Return mapped venue points and building outlines from an OSM PBF.

    Filtering happens in libosmium before Python sees an object.  Walking every
    UK building made the initial full-country job exceed GitHub Actions' limit.
    This keeps that native filtering but follows the node references of actual
    pub-building ways in a second, targeted pass.
    """
    try:
        import osmium
    except ImportError as error:
        raise SystemExit("Install dependencies with: pip install -r requirements.txt") from error

    venues, venue_ways = [], []

    def metadata(tags):
        amenity = tags.get("amenity")
        name = tags.get("name")
        if amenity not in VENUE_AMENITIES or not name:
            return None
        return {
            "name": name,
            "postcode": tags.get("addr:postcode", ""),
            "place": " ".join(tags.get(field, "") for field in ("addr:city", "addr:town", "addr:village", "addr:hamlet")),
        }

    class Handler(osmium.SimpleHandler):
        def node(self, node):
            venue = metadata(node.tags)
            if not venue or not node.location.valid():
                return
            venue.update({"lat": node.location.lat, "lng": node.location.lon})
            venues.append(venue)

        def way(self, way):
            venue = metadata(way.tags)
            refs = [int(node.ref) for node in way.nodes]
            if venue and refs:
                venue_ways.append((venue, refs))

    handler = Handler()
    entity_types = osmium.osm.NODE | osmium.osm.WAY
    reader = osmium.io.Reader(str(path), entity_types)
    amenity_filter = osmium.filter.KeyFilter("amenity").enable_for(entity_types)
    osmium.apply(reader, amenity_filter, handler)

    wanted_node_ids = {ref for _, refs in venue_ways for ref in refs}
    locations = {}

    class LocationHandler(osmium.SimpleHandler):
        def node(self, node):
            if node.location.valid():
                locations[int(node.id)] = (node.location.lat, node.location.lon)

    if wanted_node_ids:
        reader = osmium.io.Reader(str(path), osmium.osm.NODE)
        wanted_filter = osmium.filter.IdFilter(wanted_node_ids).enable_for(osmium.osm.NODE)
        osmium.apply(reader, wanted_filter, LocationHandler())
    for venue, refs in venue_ways:
        points = [locations[ref] for ref in refs if ref in locations]
        if points:
            venue.update({
                "lat": sum(point[0] for point in points) / len(points),
                "lng": sum(point[1] for point in points) / len(points),
            })
            venues.append(venue)
    return venues


def index_venues(venues):
    indexed = {}
    for venue in venues:
        indexed.setdefault(normalise(venue["name"]), []).append(venue)
    return indexed


def choose_match(row, candidates, postcode_coordinates):
    """Return a high-confidence actual venue coordinate, never a name-only guess."""
    target = postcode_coordinates.get(row["postcode"])
    expected_postcode = compact_postcode(row["postcode"])
    place = normalise(row["place_name"])
    ranked = []
    for candidate in candidates:
        score = 200  # exact normalised pub name (the index guarantees this)
        candidate_postcode = compact_postcode(candidate["postcode"])
        if candidate_postcode == expected_postcode:
            score += 1000
        if place and place in normalise(candidate["place"]):
            score += 100
        distance = distance_km(target, candidate) if target else None
        if distance is not None:
            if distance <= 1:
                score += 100
            elif distance <= 5:
                score += 70
            elif distance <= 15:
                score += 25
        ranked.append((score, distance if distance is not None else float("inf"), candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1]))
    best_score, best_distance, best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    # Exact address postcode is decisive. Otherwise accept only a nearby unique
    # same-name match; duplicate pub names must remain on their postcode pin.
    if compact_postcode(best["postcode"]) == expected_postcode:
        pass
    elif best_distance <= 5 and (not runner_up or runner_up[1] - best_distance >= 1):
        pass
    elif best_score >= 300 and best_distance <= 15 and (not runner_up or best_score - runner_up[0] >= 100):
        pass
    else:
        return None
    return {"lat": best["lat"], "lng": best["lng"], "source": "openstreetmap-gb-extract"}


def read_rows(path):
    return list(csv.DictReader(Path(path).open(encoding="utf-8", newline="")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pbf_path")
    parser.add_argument("csv_path", nargs="?", default="pubs.csv")
    parser.add_argument("output", nargs="?", default="venue-coordinates.json")
    parser.add_argument("--existing", default="venue-coordinates.json")
    parser.add_argument("--postcode-coordinates", default="pub-coordinates.json")
    args = parser.parse_args()

    rows = read_rows(args.csv_path)
    existing_path = Path(args.existing)
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {"venues": {}}
    listing_keys = {key(row) for row in rows}
    venues = {venue_key: value for venue_key, value in existing.get("venues", {}).items() if venue_key in listing_keys}
    postcode_coordinates = json.loads(Path(args.postcode_coordinates).read_text(encoding="utf-8")).get("coordinates", {})
    indexed = index_venues(read_osm_venues(args.pbf_path))
    matched = 0
    unresolved = []
    for row in rows:
        venue_key = key(row)
        candidates = []
        for name in name_variants(row["pub_name"]):
            candidates.extend(indexed.get(normalise(name), []))
        # A venue can match two name variants; retain one copy for ranking.
        candidates = list({(item["name"], item["lat"], item["lng"]): item for item in candidates}.values())
        match = choose_match(row, candidates, postcode_coordinates)
        if match:
            venues[venue_key] = match
            matched += 1
        elif venue_key not in venues:
            unresolved.append(venue_key)
    Path(args.output).write_text(
        json.dumps({"venues": venues, "unresolved_venues": sorted(unresolved), "backfill_cursor": 0}, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Matched {matched:,} listings from the OSM extract; wrote {len(venues):,} precise venue coordinates.")


if __name__ == "__main__":
    main()
