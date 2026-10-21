"""Apply a repository-owner review, with source-checked location evidence."""
import argparse
import copy
import csv
import json
import os
import re
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import unquote_plus, urlparse

from coordinate_sources import fetch_text, name_matches, parse_camra
from directory_release import (
    distance_metres, point_valid, read_json, read_rows, validate_release,
    venue_key, write_json, write_release,
)


VENUE_ID = r"v-[0-9a-f-]{36}"
CAMRA_COMMAND = re.compile(
    rf"^/verify-location (?P<venue_id>{VENUE_ID}) "
    r"(?P<url>https://camra\.org\.uk/pubs/[a-z0-9-]+)$"
)
GOOGLE_COMMAND = re.compile(
    rf'^/verify-location (?P<venue_id>{VENUE_ID}) google '
    r'(?P<lat>-?\d+(?:\.\d+)?) (?P<lng>-?\d+(?:\.\d+)?) '
    r'(?P<url>https://www\.google\.com/maps/place/\S+) '
    r'"(?P<reference_name>[^"]+)" "(?P<reference_address>[^"]+)"$'
)
MANUAL_CHECK_FIELDS = [
    "venue_key", "lat", "lng", "reference_name", "reference_address",
    "source", "source_url", "corroborating_source_url", "checked_at",
    "review_note",
]


def approved_commands(event, owner):
    comment = event.get("comment", {})
    user = comment.get("user", {})
    if user.get("login") != owner or user.get("type") != "User" or event.get("issue", {}).get("number") != 5:
        raise ValueError("Only the repository owner can approve locations in the coordinate review issue")
    lines = comment.get("body", "").strip().splitlines()
    commands = []
    for line in lines:
        line = line.strip()
        camra = CAMRA_COMMAND.fullmatch(line)
        if camra:
            commands.append({"kind": "camra", "venue_id": camra["venue_id"], "url": camra["url"]})
            continue
        google = GOOGLE_COMMAND.fullmatch(line)
        if google:
            commands.append({
                "kind": "google",
                "venue_id": google["venue_id"],
                "lat": float(google["lat"]),
                "lng": float(google["lng"]),
                "url": google["url"],
                "reference_name": google["reference_name"],
                "reference_address": google["reference_address"],
            })
            continue
        raise ValueError(
            'Use /verify-location <venue-id> <CAMRA-url>, or '
            '/verify-location <venue-id> google <lat> <lng> <Google Maps place URL> '
            '"<place name>" "<full address>"'
        )
    if not lines or len(lines) > 30:
        raise ValueError("Provide between 1 and 30 approval commands")
    if len({command["venue_id"] for command in commands}) != len(commands):
        raise ValueError("Approve each venue ID only once in a comment")
    return commands


def manual_name_matches(listing, reference):
    generic = {
        "the", "bar", "pub", "inn", "hotel", "tavern", "restaurant",
        "brewery", "taproom", "club", "social", "house", "public",
    }
    tokens = lambda value: {
        token for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in generic and not token.isdigit()
    }
    normalized_listing = re.sub(r"\s*\(PMC\)", "", listing, flags=re.I)
    # Parenthetical text often identifies a club or alias, while Google uses
    # the venue's primary name. Keep the primary listing name as the match key.
    primary_name = normalized_listing.split("(", 1)[0].strip()
    wanted = tokens(primary_name)
    actual = tokens(reference.split(",")[0].split(" - ")[0])
    return bool(wanted) and wanted.issubset(actual)


def validate_google_place(command, row, centre):
    """Require an exact named-place marker, matching name/address, and local coordinates."""
    parsed = urlparse(command["url"])
    if parsed.scheme != "https" or parsed.netloc != "www.google.com" or not parsed.path.startswith("/maps/place/"):
        raise ValueError("Google review must link to a named Google Maps place")
    slug = unquote_plus(parsed.path.split("/maps/place/", 1)[1].split("/@", 1)[0])
    if re.sub(r"[^a-z0-9]", "", slug.casefold()) != re.sub(
        r"[^a-z0-9]", "", command["reference_name"].casefold()
    ):
        raise ValueError("Reference name does not match the Google Maps place URL")
    if not manual_name_matches(row["pub_name"], command["reference_name"]):
        raise ValueError("Google place name does not identify the listed venue")
    compact = lambda value: re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if compact(row["postcode"]) not in compact(command["reference_address"]):
        raise ValueError("Google reference address must contain the listing's full postcode")
    if not point_valid({"lat": command["lat"], "lng": command["lng"]}):
        raise ValueError("Google location coordinates are invalid")
    place_points = re.findall(
        r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",
        command["url"],
    )
    target = {"lat": command["lat"], "lng": command["lng"]}
    if not place_points or min(
        distance_metres(target, {"lat": float(lat), "lng": float(lng)})
        for lat, lng in place_points
    ) > 1:
        raise ValueError("Coordinates must match the named Google Maps place marker")
    if distance_metres(target, centre) > 6000:
        raise ValueError("Google place is more than 6 km from the listed area; requires address review")


def stage_manual_checks(root, staging, reviews):
    """Keep the manually accepted point and its production override in sync."""
    root, staging = Path(root), Path(staging)
    checks_path = root / "audit/manual-place-checks.csv"
    if checks_path.exists():
        with checks_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or MANUAL_CHECK_FIELDS
            rows = list(reader)
    else:
        fieldnames, rows = MANUAL_CHECK_FIELDS, []
    overrides_payload = read_json(root / "venue-coordinate-overrides.json", {"venues": {}})
    overrides = overrides_payload.setdefault("venues", {})
    generated = read_json(root / "venue-coordinates.json", {"venues": {}}).get("venues", {})

    for review in reviews:
        key = review["venue_key"]
        lat, lng = review["lat"], review["lng"]
        previous = overrides.get(key) or generated.get(key)
        audit_row = {
            "venue_key": key,
            "lat": str(lat),
            "lng": str(lng),
            "reference_name": review["reference_name"],
            "reference_address": review["reference_address"],
            "source": "google-maps-named-place",
            "source_url": review["source_url"],
            "corroborating_source_url": "",
            "checked_at": review["checked_at"],
            "review_note": "Repository owner confirmed the named-place marker and full address in issue #5.",
        }
        existing = next((index for index, row in enumerate(rows) if row.get("venue_key") == key), None)
        if existing is None:
            rows.append(audit_row)
        else:
            rows[existing] = audit_row

        override = {
            "lat": lat,
            "lng": lng,
            "source": audit_row["source"],
            "source_url": audit_row["source_url"],
            "checked_at": audit_row["checked_at"],
            "audit_reason": "manual_named_place_address_and_map_check",
            "reference_name": audit_row["reference_name"],
            "reference_address": audit_row["reference_address"],
            "review_note": audit_row["review_note"],
        }
        if previous:
            override.update({
                "previous_lat": float(previous["lat"]),
                "previous_lng": float(previous["lng"]),
                "previous_source": previous.get("source", "venue"),
                "previous_difference_metres": round(
                    distance_metres(previous, {"lat": lat, "lng": lng}), 1
                ),
            })
        else:
            override["previous_source"] = "postcode"
        overrides[key] = override

    staging.mkdir(parents=True, exist_ok=True)
    write_json(staging / "venue-coordinate-overrides.json", overrides_payload)
    audit_staging = staging / "audit"
    audit_staging.mkdir(parents=True, exist_ok=True)
    with (audit_staging / "manual-place-checks.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def approve(root, staging, event, owner):
    commands = approved_commands(event, owner)
    root, staging = Path(root), Path(staging)
    errors = validate_release(root)
    if errors:
        raise ValueError("; ".join(errors))
    rows = read_rows(root / "pubs.csv")
    active = {r["venue_id"]: r for r in rows}
    registry = copy.deepcopy(read_json(root / "venue-registry.json"))
    ledger = read_json(root / "coordinate-verification.json")
    manual_reviews = []

    for command in commands:
        identifier = command["venue_id"]
        if identifier not in active:
            raise ValueError("Venue ID is absent from the current directory")
        row, entry = active[identifier], registry["venues"][identifier]
        if any(r.get("comment_id") == event["comment"]["id"] for r in entry.get("location_approvals", [])):
            continue
        if ledger["records"][entry["current_key"]]["display_precision"] == "venue":
            raise ValueError("Only an unverified location can be approved; verified relocations require a dedicated review")
        centre = read_json(root / "pub-coordinates.json")["coordinates"][row["postcode"]]

        if command["kind"] == "camra":
            url = command["url"]
            source = parse_camra(fetch_text(url))
            proposals = entry.get("location_research", {}).get("proposals", [])
            proposed = next((p for p in proposals if p["url"] == url), None)
            if not name_matches(row["pub_name"], source["name"], row.get("place_name", "")):
                if not proposed or not (proposed["name"] == source["name"] or name_matches(proposed["name"], source["name"], row.get("place_name", ""))):
                    raise ValueError("Source name does not identify this pub or a recorded research candidate")
            if proposed and (distance_metres(source, proposed) > 50 or (proposed["postcode"] and source["postcode"] != proposed["postcode"])):
                raise ValueError("Source address/coordinate changed since research; refresh the candidate before approving")
            if distance_metres(source, centre) > 6000:
                raise ValueError("Source is more than 6 km from the listed area; requires a dedicated relocation/address review")
            locality = source.get("locality", "").strip()
            listing_locality = row.get("place_name", "").strip()
            locality_matches = bool(locality and listing_locality and locality.casefold() == listing_locality.casefold())
            if not source.get("postcode") or (not source.get("address") and not locality_matches):
                raise ValueError("Source lacks a named street address or matching locality and full postcode")
            pin = dict(lat=source["lat"], lng=source["lng"], source="owner-reviewed-camra", evidence=[dict(
                source="camra", url=url, checked_at=date.today().isoformat(),
                match=("owner_reviewed_named_address_and_map" if source.get("address") else "owner_reviewed_named_locality_and_map"),
                reference_coordinate=dict(lat=source["lat"], lng=source["lng"]), reference_name=source["name"],
                reference_address=source.get("address", ""), reference_locality=locality,
                reference_postcode=source["postcode"],
                reviewed_by=owner, review_url=event["comment"]["html_url"])])
            if row["postcode"].replace(" ", "").upper() != source["postcode"].replace(" ", "").upper():
                pin["metadata_issue"] = dict(field="postcode", listed=row["postcode"], reference=source["postcode"], source_url=url)
            source_name = source["name"].split(",")[0].split(" - ")[0].strip()
            for name in (row["pub_name"], source_name):
                key = venue_key(dict(row, pub_name=name, postcode=source["postcode"]))
                if key not in entry["aliases"]:
                    entry["aliases"].append(key)
            approval_url = url
        else:
            validate_google_place(command, row, centre)
            checked_at = date.today().isoformat()
            reference_coordinate = {"lat": command["lat"], "lng": command["lng"]}
            pin = dict(
                lat=command["lat"], lng=command["lng"], source="owner-reviewed-google-maps",
                evidence=[dict(
                    source="google-maps-named-place",
                    url=command["url"],
                    checked_at=checked_at,
                    match="owner_reviewed_named_place_and_full_postcode",
                    reference_coordinate=reference_coordinate,
                    reference_name=command["reference_name"],
                    reference_address=command["reference_address"],
                    reference_postcode=row["postcode"],
                    reviewed_by=owner,
                    review_url=event["comment"]["html_url"],
                )],
            )
            approval_url = command["url"]
            manual_reviews.append({
                "venue_key": entry["current_key"],
                "lat": command["lat"],
                "lng": command["lng"],
                "reference_name": command["reference_name"],
                "reference_address": command["reference_address"],
                "source_url": command["url"],
                "checked_at": checked_at,
            })

        previous = copy.deepcopy(entry.get("verified_pin"))
        entry["verified_pin"] = pin
        entry.setdefault("location_approvals", []).append(dict(
            comment_id=event["comment"]["id"],
            review_url=event["comment"]["html_url"],
            reviewed_by=owner,
            checked_at=date.today().isoformat(),
            source_url=approval_url,
            previous_pin=previous,
        ))

    staging.mkdir(parents=True, exist_ok=True)
    write_release(staging, rows, registry, read_json(root / "directory-meta.json"), read_json(root / "pub-coordinates.json")["coordinates"])
    # Keep non-release audit inputs available to the candidate validators.
    overrides_path = root / "venue-coordinate-overrides.json"
    if overrides_path.exists():
        shutil.copyfile(overrides_path, staging / "venue-coordinate-overrides.json")
    else:
        write_json(staging / "venue-coordinate-overrides.json", {"venues": {}})
    (staging / "audit").mkdir(parents=True, exist_ok=True)
    checks_path = root / "audit/manual-place-checks.csv"
    if checks_path.exists():
        shutil.copyfile(checks_path, staging / "audit/manual-place-checks.csv")
    else:
        with (staging / "audit/manual-place-checks.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANUAL_CHECK_FIELDS, lineterminator="\n")
            writer.writeheader()
    if manual_reviews:
        stage_manual_checks(root, staging, manual_reviews)
    shutil.copyfile(root / "latest-bass-directory.pdf", staging / "latest-bass-directory.pdf")
    errors = validate_release(staging, root)
    if errors:
        raise ValueError("; ".join(errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--staging", default="candidate-approval")
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    approve(args.root, args.staging, event, os.environ["GITHUB_REPOSITORY_OWNER"])
