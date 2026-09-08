"""Create auditable AI-assisted coordinate suggestions for unresolved venues.

This script never changes the live coordinate file. It produces a review queue
with a source URL and confidence score for a human to approve.
"""
import argparse
import csv
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


API_URL = "https://api.openai.com/v1/responses"


def listing_key(row):
    return "|".join(row[field].strip().lower() for field in ("pub_name", "place_name", "postcode"))


def request_suggestion(row, model):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "latitude", "longitude", "address", "source_url", "confidence", "reason"],
        "properties": {
            "verdict": {"type": "string", "enum": ["match", "no_match"]},
            "latitude": {"type": ["number", "null"]},
            "longitude": {"type": ["number", "null"]},
            "address": {"type": "string"},
            "source_url": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
    }
    prompt = (
        "Find this UK pub's actual venue location using web search. Do not guess. "
        "Only return match when a source identifies the same venue and its location agrees with the town or postcode. "
        f"Pub: {row['pub_name']}; place: {row['place_name']}; postcode: {row['postcode']}. "
        "For no_match, use null coordinates, an empty source_url, and explain why."
    )
    payload = {
        "model": model,
        "store": False,
        "tools": [{"type": "web_search"}],
        "input": prompt,
        "text": {"format": {"type": "json_schema", "name": "venue_resolution", "strict": True, "schema": schema}},
    }
    request = Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=90) as response:
        data = json.loads(response.read())
    return json.loads(data["output_text"])


def main():
    parser = argparse.ArgumentParser(description="Build an AI-assisted review queue for unresolved Bass venues.")
    parser.add_argument("csv_path", nargs="?", default="pubs.csv")
    parser.add_argument("--coordinates", default="venue-coordinates.json")
    parser.add_argument("--output", default="ai-venue-review.json")
    parser.add_argument("--limit", type=int, default=25, help="Maximum unresolved venues to review; 0 means all.")
    parser.add_argument("--model", default=os.environ.get("OPENAI_RESOLVER_MODEL", "") or "gpt-5")
    args = parser.parse_args()

    rows = list(csv.DictReader(Path(args.csv_path).open(encoding="utf-8", newline="")))
    coordinates = json.loads(Path(args.coordinates).read_text(encoding="utf-8"))
    unresolved = set(coordinates.get("unresolved_venues", []))
    targets = [row for row in rows if listing_key(row) in unresolved]
    if args.limit:
        targets = targets[:args.limit]
    reviews = []
    for index, row in enumerate(targets, start=1):
        entry = {"listing": row, "key": listing_key(row)}
        try:
            entry["suggestion"] = request_suggestion(row, args.model)
        except Exception as error:
            entry["error"] = str(error)
        reviews.append(entry)
        print(f"{index}/{len(targets)}: {row['pub_name']}")
    report = {"model": args.model, "review_count": len(reviews), "reviews": reviews}
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
