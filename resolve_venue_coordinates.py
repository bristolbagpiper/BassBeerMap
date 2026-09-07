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
USER_AGENT = "BassBeerMap venue-coordinate resolver (github.com/bristolbagpiper/BassBeerMap)"


def key(row):
    return "|".join(row[field].strip().lower() for field in ("pub_name", "place_name", "postcode"))


def normalise(value):
    value = re.sub(r"^the\s+", "", value.strip().lower())
    return re.sub(r"[^a-z0-9]", "", value)


def resolve(row):
    query = f"{row['pub_name']}, {row['place_name']}, {row['postcode']}, United Kingdom"
    url = f"{API_URL}?{urlencode({'q': query, 'format': 'jsonv2', 'addressdetails': 1, 'limit': 5, 'countrycodes': 'gb,im,je'})}"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        candidates = json.loads(response.read())
    wanted = normalise(row["pub_name"])
    for candidate in candidates:
        if normalise(candidate.get("name", "")) == wanted and candidate.get("type") in {"pub", "bar", "social_club", "club"}:
            return {"lat": float(candidate["lat"]), "lng": float(candidate["lon"]), "source": "openstreetmap"}
    return None


def read_rows(path):
    return list(csv.DictReader(Path(path).open(encoding="utf-8", newline="")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", default="pubs.csv")
    parser.add_argument("output", nargs="?", default="venue-coordinates.json")
    parser.add_argument("--existing", default="venue-coordinates.json")
    parser.add_argument("--previous", help="Only resolve listings not present in this CSV.")
    args = parser.parse_args()

    rows = read_rows(args.csv_path)
    listing_keys = {key(row) for row in rows}
    previous_keys = {key(row) for row in read_rows(args.previous)} if args.previous else set()
    existing_path = Path(args.existing)
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {"venues": {}}
    venues = {venue_key: value for venue_key, value in existing.get("venues", {}).items() if venue_key in listing_keys}
    unresolved = []
    candidates = [row for row in rows if key(row) not in previous_keys and key(row) not in venues]

    for index, row in enumerate(candidates, start=1):
        venue_key = key(row)
        match = None
        try:
            match = resolve(row)
            if match:
                venues[venue_key] = match
            else:
                unresolved.append(venue_key)
        except Exception as error:
            unresolved.append(venue_key)
            print(f"{index}: lookup failed for {row['pub_name']}: {error}")
        print(f"{index}/{len(candidates)}: {row['pub_name']} {'matched' if match else 'not matched'}")
        if index < len(candidates):
            time.sleep(1.1)

    Path(args.output).write_text(json.dumps({"venues": venues, "unresolved_venues": unresolved}, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(venues):,} precise venue coordinates; {len(unresolved):,} new listings need review.")


if __name__ == "__main__":
    main()
