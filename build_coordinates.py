"""Build browser-ready postcode coordinates during the data update, not per visitor."""
import argparse
import csv
import json
from pathlib import Path
from urllib.request import Request, urlopen

API_URL = "https://api.postcodes.io/postcodes"
USER_AGENT = "BassBeerMapBot/1.0"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", default="pubs.csv")
    parser.add_argument("output", nargs="?", default="pub-coordinates.json")
    parser.add_argument("--overrides", default="postcode-overrides.json")
    args = parser.parse_args()
    rows = list(csv.DictReader(Path(args.csv_path).open(encoding="utf-8", newline="")))
    postcodes = sorted({row["postcode"].strip() for row in rows if row["postcode"].strip()})
    overrides = json.loads(Path(args.overrides).read_text(encoding="utf-8")) if Path(args.overrides).exists() else {}
    coordinates = {}
    for postcode, item in overrides.items():
        if item.get("lat") is not None and item.get("lng") is not None:
            coordinates[item.get("postcode", postcode)] = {"lat": item["lat"], "lng": item["lng"]}
    unresolved = [postcode for postcode in postcodes if postcode not in coordinates]
    for offset in range(0, len(unresolved), 100):
        payload = json.dumps({"postcodes": unresolved[offset:offset + 100]}).encode()
        request = Request(API_URL, data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Postcode lookup returned HTTP {response.status}")
            result = json.loads(response.read())["result"]
        for entry in result:
            value = entry.get("result") or {}
            if value.get("latitude") is not None and value.get("longitude") is not None:
                coordinates[entry["query"]] = {"lat": value["latitude"], "lng": value["longitude"]}
    missing = sorted(set(postcodes) - set(coordinates))
    Path(args.output).write_text(json.dumps({"coordinates": coordinates, "unmapped_postcodes": missing}, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(coordinates):,} coordinates; {len(missing)} postcodes remain unmapped.")
    if missing:
        raise SystemExit(f"Unmapped postcodes require a correction or coordinate override: {', '.join(missing)}")


if __name__ == "__main__":
    main()
