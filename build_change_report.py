"""Create an auditable listing-by-listing report for a directory refresh."""
import argparse
import csv
import hashlib
import io
import json
import subprocess
from urllib.parse import urlencode
from pathlib import Path
from datetime import date

from build_coordinate_verification import venue_key
from directory_release import MAX_EVIDENCE_AGE_DAYS, active_evidence, distance_metres, point_valid
from location_review import notification_items


DISPLAY_FIELDS = ("country", "area", "pub_name", "place_name", "postcode", "pg", "last", "dispense", "notes")
COMPARE_FIELDS = DISPLAY_FIELDS


def read_rows(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def listing_key(row):
    if row.get('venue_id'):
        return row['venue_id']
    return "|".join(str(row.get(field, "")).strip().casefold() for field in ("pub_name", "place_name", "postcode"))


def public_row(row):
    return {field: str(row.get(field, "")).strip() for field in DISPLAY_FIELDS}


def build_report(previous_rows, candidate_rows):
    # Bootstrap reports can compare a legacy CSV to its first ID-bearing import.
    if not all(row.get('venue_id') for row in previous_rows):
        candidate_rows = [dict(row, venue_id='') for row in candidate_rows]
    previous = {listing_key(row): row for row in previous_rows}
    candidate = {listing_key(row): row for row in candidate_rows}
    added = [public_row(candidate[key]) for key in sorted(candidate.keys() - previous.keys())]
    removed = [public_row(previous[key]) for key in sorted(previous.keys() - candidate.keys())]
    updated = []
    for key in sorted(previous.keys() & candidate.keys()):
        changes = {
            field: {"previous": str(previous[key].get(field, "")).strip(), "candidate": str(candidate[key].get(field, "")).strip()}
            for field in COMPARE_FIELDS
            if str(previous[key].get(field, "")).strip() != str(candidate[key].get(field, "")).strip()
        }
        if changes:
            updated.append({"listing": public_row(candidate[key]), "changes": changes})
    return {
        "summary": {
            "previous_listing_count": len(previous_rows),
            "candidate_listing_count": len(candidate_rows),
            "added_count": len(added),
            "removed_count": len(removed),
            "updated_count": len(updated),
            "has_changes": bool(added or removed or updated),
        },
        "added": added,
        "removed": removed,
        "updated": updated,
    }


def _git_show(commit, path):
    result = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parent), "show", f"{commit}:{path}"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout


def _snapshot(commit):
    files = {
        "rows": "pubs.csv",
        "pdf": "latest-bass-directory.pdf",
        "metadata": "directory-meta.json",
        "bundle": "directory-release.json",
        "ledger": "coordinate-verification.json",
        "review": "location-review.json",
    }
    raw = {key: _git_show(commit, name) for key, name in files.items()}
    return {
        "rows": list(csv.DictReader(io.StringIO(raw["rows"].decode("utf-8-sig")))),
        "pdf": raw["pdf"],
        "metadata": json.loads(raw["metadata"]),
        "bundle": json.loads(raw["bundle"]),
        "ledger": json.loads(raw["ledger"]),
        "review": json.loads(raw["review"]),
    }


def _effective_search_url(row, location):
    issue = location.get("metadata_issue") or {}
    postcode = (issue.get("reference") if issue.get("use_reference_for_directions")
                and issue.get("listed") == row.get("postcode") else row.get("postcode", ""))
    name = (location.get("alternate_name") or row.get("pub_name", "")).removesuffix(" (PMC)")
    address = location.get("directions_address") or {}
    if address.get("street"):
        street = address["street"]
        address_postcode = address.get("postcode") or postcode
        locality = row.get("place_name", "").split(":", 1)[0].strip()
        has_postcode = address_postcode and address_postcode.casefold() in street.casefold()
        has_locality = any(part.strip().casefold() == locality.casefold() for part in street.split(","))
        query = f"{name}, {street}" if has_postcode else ", ".join(
            value for value in (name, street, "" if has_locality else locality, address_postcode) if value)
    else:
        query = ", ".join(value for value in (name, row.get("place_name", ""), postcode) if value)
    params = {"api": "1", "query": query}
    return "https://www.google.com/maps/search/?" + urlencode(params)


def _location_by_id(snapshot):
    return snapshot["bundle"].get("locations", {})


def _evidence(snapshot, row, published_on):
    key = venue_key(row)
    record = snapshot["ledger"].get("records", {}).get(key, {})
    output = []
    try:
        as_of = date.fromisoformat(str(published_on)[:10])
    except ValueError:
        as_of = date.today()
    names = {
        "camra": "CAMRA", "food_standards_agency": "Food Standards Agency",
        "manual_override": "Reviewed location source",
    }
    for item in active_evidence(record):
        source = str(item.get("source", "")).strip()
        url = str(item.get("url", "")).strip()
        if (not source or not url.startswith("https://") or "google" in (source + url).casefold()):
            continue
        try:
            checked = date.fromisoformat(item.get("checked_at", ""))
        except (TypeError, ValueError):
            continue
        if not 0 <= (as_of - checked).days <= MAX_EVIDENCE_AGE_DAYS:
            continue
        label = names.get(source.casefold(), source.replace("_", " ").title())
        output.append({"name": label, "url": url})
    unique = {}
    for item in output:
        unique.setdefault((item["name"].casefold(), item["url"]), item)
    output = list(unique.values())
    output.sort(key=lambda item: (item["name"].casefold(), item["url"]))
    return output


def _effective_postcode(row, location):
    address = location.get("directions_address") or {}
    if address.get("postcode"):
        return address["postcode"]
    issue = location.get("metadata_issue") or {}
    if issue.get("use_reference_for_directions") and issue.get("listed") == row.get("postcode"):
        return issue.get("reference") or row.get("postcode", "")
    return row.get("postcode", "")


def _pin_confidence(location, evidence):
    if location.get("precision") != "venue":
        return "Approximate postcode location; no independently supported venue pin."
    if evidence:
        return "Venue pin has current independent support."
    return "Venue pin has no current independent evidence listed."


def _listing_context(row, snapshot, published_on):
    identifier = row.get("venue_id", "")
    location = _location_by_id(snapshot).get(identifier, {})
    google_listing = location.get("google_listing") or {}
    maps_url = google_listing.get("maps_url")
    if not maps_url and google_listing.get("place_id"):
        params = {"api": "1", "query": row.get("pub_name", "") + ", " + row.get("place_name", "")}
        params["query_place_id"] = google_listing["place_id"]
        maps_url = "https://www.google.com/maps/search/?" + urlencode(params)
    is_direct = bool(google_listing)
    maps_url = maps_url or _effective_search_url(row, location)
    evidence = _evidence(snapshot, row, published_on)
    ledger_record = snapshot["ledger"].get("records", {}).get(venue_key(row), {})
    precision = location.get("precision", ledger_record.get("display_precision", "postcode"))
    premises = location.get("premises_precision")
    if not premises:
        premises = "postcode" if precision != "venue" else "site"
    return {
        "location": {
            "lat": location.get("lat"), "lng": location.get("lng"),
            "precision": precision, "premises_precision": premises,
            "street": (location.get("directions_address") or {}).get("street"),
            "effective_postcode": _effective_postcode(row, location),
            "visitor_hint": location.get("visitor_hint"),
            "confidence": _pin_confidence(location, evidence),
        },
        "evidence": evidence,
        "maps_url": maps_url,
        "maps_label": "Open confirmed Google listing" if is_direct else "Search Google Maps",
    }


def _friendly_precision(value):
    return {
        "venue": "venue pin", "postcode": "approximate postcode location",
        "building": "inside the identified building", "reference": "reference location",
        "site": "site location; serving point unconfirmed",
    }.get(value, "location")


def _coordinate_change(before, after):
    old, new = {"lat": before.get("lat"), "lng": before.get("lng")}, {"lat": after.get("lat"), "lng": after.get("lng")}
    if not (point_valid(old) and point_valid(new)):
        return {"previous": "no venue pin" if not point_valid(old) else old,
                "candidate": "no venue pin" if not point_valid(new) else new,
                "distance_metres": None}
    return {"previous": old, "candidate": new,
            "distance_metres": round(distance_metres(old, new))}


def published_report(commit, previous_commit, published_on):
    """Build the notification from the exact validated and prior published Git snapshots."""
    current = _snapshot(commit)
    previous = _snapshot(previous_commit)
    repository = Path(__file__).resolve().parent
    full_commit = subprocess.run(["git", "-C", str(repository), "rev-parse", commit], check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    full_previous_commit = subprocess.run(["git", "-C", str(repository), "rev-parse", previous_commit], check=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    comparison = build_report(previous["rows"], current["rows"])
    current_by_id = {row.get("venue_id"): row for row in current["rows"] if row.get("venue_id")}
    previous_by_id = {row.get("venue_id"): row for row in previous["rows"] if row.get("venue_id")}
    added_ids = sorted(current_by_id.keys() - previous_by_id.keys())
    removed_ids = sorted(previous_by_id.keys() - current_by_id.keys())
    report = {
        "summary": dict(comparison["summary"]),
        "edition": current["metadata"].get("directory_label") or current["metadata"].get("directory_month_year", "Unknown edition"),
        "published_on": published_on,
        "commit": full_commit,
        "live_url": "https://bassbeermap.com/",
        "publication": {
        "commit": full_commit,
        "previous_commit": full_previous_commit,
        "pdf_changed": hashlib.sha256(current["pdf"]).digest() != hashlib.sha256(previous["pdf"]).digest(),
        },
    }
    report["summary"]["pdf_changed"] = report["publication"]["pdf_changed"]
    report["summary"]["has_changes"] = bool(report["summary"]["has_changes"] or report["publication"]["pdf_changed"])
    report["added_publications"] = []
    for identifier in added_ids:
        row = current_by_id[identifier]
        report["added_publications"].append({"listing": public_row(row), **_listing_context(row, current, published_on)})
    report["removed_publications"] = []
    for identifier in removed_ids:
        row = previous_by_id[identifier]
        report["removed_publications"].append({"listing": public_row(row), **_listing_context(row, previous, published_on)})
    field_changes = []
    current_locations, previous_locations = _location_by_id(current), _location_by_id(previous)
    for identifier in sorted(current_by_id.keys() & previous_by_id.keys()):
        before, after = previous_by_id[identifier], current_by_id[identifier]
        before_location, after_location = previous_locations.get(identifier, {}), current_locations.get(identifier, {})
        fields = {}
        labels = {
            "pub_name": "Pub name", "place_name": "Town or locality", "postcode": "Directory postcode",
            "country": "Country", "area": "Region", "pg": "Bass classification",
            "dispense": "Dispense method", "notes": "Directory notes", "last": "Last Bass confirmation",
        }
        for field in ("pub_name", "place_name", "postcode", "country", "area", "pg", "dispense", "notes", "last"):
            old, new = str(before.get(field, "")).strip(), str(after.get(field, "")).strip()
            if old != new:
                if field == "pg":
                    old = {"Perm": "Permanent"}.get(old, old)
                    new = {"Perm": "Permanent"}.get(new, new)
                fields[labels[field]] = {"previous": old or "not recorded", "candidate": new or "not recorded"}
        old_effective, new_effective = _effective_postcode(before, before_location), _effective_postcode(after, after_location)
        if (old_effective != new_effective and
                (old_effective != before.get("postcode") or new_effective != after.get("postcode"))):
            fields["Effective directions postcode"] = {"previous": old_effective or "not listed", "candidate": new_effective or "not listed"}
        old_street = (before_location.get("directions_address") or {}).get("street")
        new_street = (after_location.get("directions_address") or {}).get("street")
        if old_street != new_street:
            fields["Street address"] = {"previous": old_street or "not recorded", "candidate": new_street or "not recorded"}
        old_hint, new_hint = before_location.get("visitor_hint"), after_location.get("visitor_hint")
        if old_hint != new_hint and (old_hint or new_hint):
            fields["Visitor directions"] = {"previous": old_hint or "not recorded", "candidate": new_hint or "not recorded"}
        old_alternate, new_alternate = before_location.get("alternate_name"), after_location.get("alternate_name")
        if old_alternate != new_alternate and (old_alternate or new_alternate):
            fields["Trading name"] = {"previous": old_alternate or "not recorded", "candidate": new_alternate or "not recorded"}
        old_precision = before_location.get("precision", "postcode")
        new_precision = after_location.get("precision", "postcode")
        if old_precision != new_precision:
            fields["Location type"] = {"previous": _friendly_precision(old_precision), "candidate": _friendly_precision(new_precision)}
        old_premises = before_location.get("premises_precision")
        new_premises = after_location.get("premises_precision")
        if old_premises != new_premises and (old_premises or new_premises):
            fields["Pin accuracy"] = {"previous": _friendly_precision(old_premises), "candidate": _friendly_precision(new_premises)}
        old_evidence = _evidence(previous, before, published_on)
        new_evidence = _evidence(current, after, published_on)
        old_confidence = _pin_confidence(before_location, old_evidence)
        new_confidence = _pin_confidence(after_location, new_evidence)
        if old_confidence != new_confidence:
            fields["Location support"] = {"previous": old_confidence, "candidate": new_confidence}
        old_point = {"lat": before_location.get("lat"), "lng": before_location.get("lng")}
        new_point = {"lat": after_location.get("lat"), "lng": after_location.get("lng")}
        if old_point != new_point:
            fields["Coordinates"] = _coordinate_change(before_location, after_location)
        if fields:
            field_changes.append({"listing": public_row(after), "changes": fields})
    report["field_changes_by_venue_id"] = field_changes
    report["actionable_owner_items"] = notification_items(current["review"])
    return report


def main():
    parser = argparse.ArgumentParser(description="Compare the current and candidate Bass listings.")
    parser.add_argument("candidate")
    parser.add_argument("--previous", default="pubs.csv")
    parser.add_argument("--output", default="change-report.json")
    args = parser.parse_args()
    report = build_report(read_rows(args.previous), read_rows(args.candidate))
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
