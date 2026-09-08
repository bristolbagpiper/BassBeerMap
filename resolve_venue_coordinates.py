"""Create precise OpenStreetMap coordinates for newly added directory venues."""
import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "BassBeerMap venue-coordinate resolver (github.com/bristolbagpiper/BassBeerMap)"


def key(row):
    return "|".join(row[field].strip().lower() for field in ("pub_name", "place_name", "postcode"))


def normalise(value):
    value = re.sub(r"^the\s+", "", value.strip().lower()).replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", value)


def name_variants(name):
    variants = [name]
    if re.search(r"\band\b", name, flags=re.IGNORECASE):
        variants.append(re.sub(r"\band\b", "&", name, flags=re.IGNORECASE))
    if name.lower().startswith("the "):
        variants.append(name[4:])
    return list(dict.fromkeys(variants))


def resolve_overpass(row, postcode_coordinates):
    postcode = postcode_coordinates.get(row["postcode"])
    if not postcode:
        return None
    query = """
[out:json][timeout:30];
(
  nwr["amenity"~"^(pub|bar|social_club|club)$"](around:5000,{lat},{lng});
);
out center tags;
""".format(lat=postcode["lat"], lng=postcode["lng"])
    request = Request(
        OVERPASS_API_URL,
        data=query.encode(),
        headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain", "Accept": "application/json"},
    )
    with urlopen(request, timeout=60) as response:
        elements = json.loads(response.read()).get("elements", [])
    wanted = normalise(row["pub_name"])
    matches = []
    for element in elements:
        if normalise(element.get("tags", {}).get("name", "")) != wanted:
            continue
        point = element.get("center", element)
        if point.get("lat") is not None and point.get("lon") is not None:
            matches.append(point)
    if len(matches) != 1:
        return None
    return {"lat": float(matches[0]["lat"]), "lng": float(matches[0]["lon"]), "source": "openstreetmap-overpass"}


def resolve(row, postcode_coordinates):
    wanted = normalise(row["pub_name"])
    for index, pub_name in enumerate(name_variants(row["pub_name"])):
        if index:
            time.sleep(1.1)
        query = f"{pub_name}, {row['place_name']}, {row['postcode']}, United Kingdom"
        url = f"{API_URL}?{urlencode({'q': query, 'format': 'jsonv2', 'addressdetails': 1, 'limit': 5, 'countrycodes': 'gb,im,je'})}"
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urlopen(request, timeout=30) as response:
            candidates = json.loads(response.read())
        for candidate in candidates:
            if normalise(candidate.get("name", "")) == wanted and candidate.get("type") in {"pub", "bar", "social_club", "club"}:
                return {"lat": float(candidate["lat"]), "lng": float(candidate["lon"]), "source": "openstreetmap"}
    time.sleep(1.1)
    return resolve_overpass(row, postcode_coordinates)


def read_rows(path):
    return list(csv.DictReader(Path(path).open(encoding="utf-8", newline="")))


def write_output(path, venues, unresolved):
    Path(path).write_text(
        json.dumps({"venues": venues, "unresolved_venues": unresolved}, separators=(",", ":")),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", default="pubs.csv")
    parser.add_argument("output", nargs="?", default="venue-coordinates.json")
    parser.add_argument("--existing", default="venue-coordinates.json")
    parser.add_argument("--previous", help="Only resolve listings not present in this CSV.")
    parser.add_argument("--postcode-coordinates", default="pub-coordinates.json")
    args = parser.parse_args()

    rows = read_rows(args.csv_path)
    listing_keys = {key(row) for row in rows}
    previous_keys = {key(row) for row in read_rows(args.previous)} if args.previous else set()
    existing_path = Path(args.existing)
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {"venues": {}}
    postcode_coordinates = json.loads(Path(args.postcode_coordinates).read_text(encoding="utf-8")).get("coordinates", {})
    venues = {venue_key: value for venue_key, value in existing.get("venues", {}).items() if venue_key in listing_keys}
    unresolved = {
        venue_key
        for venue_key in existing.get("unresolved_venues", [])
        if venue_key in listing_keys and venue_key not in venues
    }
    candidates = [row for row in rows if key(row) not in previous_keys and key(row) not in venues]

    for index, row in enumerate(candidates, start=1):
        venue_key = key(row)
        match = None
        try:
            match = resolve(row, postcode_coordinates)
            if match:
                venues[venue_key] = match
            else:
                unresolved.add(venue_key)
        except Exception as error:
            unresolved.add(venue_key)
            print(f"{index}: lookup failed for {row['pub_name']}: {error}")
        print(f"{index}/{len(candidates)}: {row['pub_name']} {'matched' if match else 'not matched'}")
        write_output(args.output, venues, sorted(unresolved))
        if index < len(candidates):
            time.sleep(1.1)

    write_output(args.output, venues, sorted(unresolved))
    print(f"Wrote {len(venues):,} precise venue coordinates; {len(unresolved):,} new listings need review.")


if __name__ == "__main__":
    main()
