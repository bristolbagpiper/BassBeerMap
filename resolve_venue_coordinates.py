"""One-time, rate-limited OpenStreetMap venue-coordinate resolver.

Keeps only exact pub-name matches; unmatched entries continue to use postcode
coordinates. Run manually, not in the scheduled workflow.
"""
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CSV_PATH = Path("pubs.csv")
OUTPUT_PATH = Path("venue-coordinates.json")
API_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "BassBeerMap venue-coordinate resolver (github.com/bristolbagpiper/BassBeerMap)"


def key(row):
    return "|".join(row[field].strip().lower() for field in ("pub_name", "place_name", "postcode"))


def normalise(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


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


def main():
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8", newline="")))
    existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {"venues": {}}
    venues = existing.setdefault("venues", {})
    for index, row in enumerate(rows, start=1):
        venue_key = key(row)
        if venue_key in venues:
            continue
        match = None
        try:
            match = resolve(row)
            if match:
                venues[venue_key] = match
        except Exception as error:
            print(f"{index}: lookup failed for {row['pub_name']}: {error}")
        OUTPUT_PATH.write_text(json.dumps(existing, separators=(",", ":")), encoding="utf-8")
        print(f"{index}/{len(rows)}: {row['pub_name']} {'matched' if match else 'not matched'}")
        time.sleep(1.1)


if __name__ == "__main__":
    main()
