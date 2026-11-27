"""Email a concise, auditable summary of a validated directory refresh."""
import argparse
import json
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path


def listing_label(row):
    return f"{row['pub_name']} — {row['place_name']} ({row['postcode']})"


def format_report(report):
    summary = report["summary"]
    lines = [
        "Bass directory release passed validation and was committed to the repository.",
        "Pages deployment and live-map availability are checked separately.",
        "",
        f"Listings: {summary['previous_listing_count']} → {summary['candidate_listing_count']}",
        f"Added: {summary['added_count']} | Removed: {summary['removed_count']} | Updated: {summary['updated_count']}",
    ]
    for heading, rows in (("New listings", report["added"]), ("Removed listings", report["removed"])):
        if rows:
            lines.extend(["", f"{heading}:"])
            lines.extend(f"- {listing_label(row)}" for row in rows)
    if report["updated"]:
        lines.extend(["", "Updated listings:"])
        for item in report["updated"]:
            changes = "; ".join(f"{field}: {value['previous']} -> {value['candidate']}" for field, value in item["changes"].items())
            lines.append(f"- {listing_label(item['listing'])}: {changes}")
    return "\n".join(lines)


def _clean(value):
    if value in (None, "", {}):
        return "not recorded"
    if isinstance(value, dict):
        return "not recorded"
    return str(value)


def _location_description(location):
    kind = location.get("precision")
    pin = location.get("premises_precision")
    if kind != "venue":
        return "Approximate postcode location"
    pin_text = {
        "building": "pin within the identified building",
        "site": "site pin; serving point is unconfirmed",
        "reference": "reference location",
    }.get(pin, "venue pin")
    return pin_text[0].upper() + pin_text[1:]


def _membership_label(value):
    return {"Perm": "Permanent", "Permanent": "Permanent", "Guest": "Guest"}.get(value, _clean(value))


def _coordinate_text(value):
    if not isinstance(value, dict) or value.get("lat") is None or value.get("lng") is None:
        return "no venue pin"
    return f"{float(value['lat']):.5f}, {float(value['lng']):.5f}"


def _format_change(name, values):
    old, new = values.get("previous"), values.get("candidate")
    if name.casefold() == "coordinates":
        text = f"Coordinates: {_coordinate_text(old)} → {_coordinate_text(new)}"
        distance = values.get("distance_metres")
        if distance is not None:
            text += f" (moved about {distance:,} m)"
        return text
    return f"{name}: {_clean(old)} → {_clean(new)}"


def format_publication(report, items=None):
    """Render the published edition as a concise owner-facing email."""
    publication = report.get("publication", {})
    pdf_changed = publication.get("pdf_changed", False)
    lines = [f"Bass directory — {report.get('edition', 'Published edition')}",
             f"Published {report.get('published_on', 'on an unrecorded date')}.",
             f"The current edition contains {report.get('summary', {}).get('candidate_listing_count', 0)} listings."]
    added = report.get("added_publications", [])
    removed = report.get("removed_publications", [])
    changes = report.get("field_changes_by_venue_id", [])
    listing_count = report.get("summary", {}).get("candidate_listing_count", 0)
    listing_word = "listing" if listing_count == 1 else "listings"
    lines[2] = f"The current edition contains {listing_count} {listing_word}."

    if not added and not removed and not changes:
        if pdf_changed:
            lines.append("The source PDF was revised. The published listings and locations stayed the same.")
        else:
            lines.append("No listing or location changes were made in this publication.")
    else:
        lines.append("The live directory includes these changes:")

    if added:
        lines.extend(["", "Added listings:"])
        for item in added:
            row = item.get("listing", {})
            location = item.get("location", {})
            lines.append(f"- {listing_label(row)} — Bass classification: {_membership_label(row.get('pg', 'not recorded'))}; "
                         f"dispense: {row.get('dispense', 'not recorded')}.")
            description = _location_description(location)
            pin = location.get("lat"), location.get("lng")
            if location.get("precision") == "venue" and None not in pin:
                description += f" at {float(pin[0]):.5f}, {float(pin[1]):.5f}"
            street = location.get("street")
            postcode = location.get("effective_postcode")
            if street:
                description += f"; {street}"
            if postcode:
                description += f" ({postcode})"
            lines.append(f"  Location: {description}.")
            hint = location.get("visitor_hint")
            if hint:
                lines.append(f"  Visitor directions: {hint}")
            lines.append(f"  {location.get('confidence', 'Location support not recorded')}")
            evidence = item.get("evidence", [])
            if evidence:
                grouped = {}
                for source in evidence:
                    grouped.setdefault(source.get("name", "Other source"), []).append(source.get("url", ""))
                rendered = "; ".join(f"{name}: {', '.join(urls)}" for name, urls in grouped.items())
                lines.append(f"  Independent sources: {rendered}")
            maps_url = item.get("maps_url")
            if maps_url:
                lines.append(f"  {item.get('maps_label', 'Google Maps')}: {maps_url}")

    if removed:
        lines.extend(["", "Removed listings:"])
        for item in removed:
            lines.append(f"- {listing_label(item.get('listing', {}))}")

    if changes:
        lines.extend(["", "Changed listings:"])
        for item in changes:
            lines.append(f"- {listing_label(item.get('listing', {}))}")
            lines.extend(f"  {_format_change(name, value)}" for name, value in item.get("changes", {}).items())

    actionable = report.get("actionable_owner_items", {}) if items is None else items
    if actionable:
        lines.extend(["", "Outstanding owner decisions:"])
        for _, item in sorted(actionable.items(), key=lambda pair: (pair[1].get("pub_name", "").casefold(), pair[0])):
            lines.append(f"- {listing_label(item)}: {item.get('question', 'Please review this listing.')}")
            context = item.get("context")
            if context:
                context = context.split(" Google address:", 1)[0]
                context = re.sub(r"https?://\S+", "", context).strip()
                if context:
                    lines.append(f"  {context}")
    else:
        lines.extend(["", "There are no outstanding owner decisions."])

    lines.extend(["", f"Live directory: {report.get('live_url', 'https://bassbeermap.com/')}"])
    return "\n".join(lines)


def send_report(report, smtp_factory=smtplib.SMTP, environ=None):
    """Send a report and fail unless the configured SMTP server accepts it."""
    environ = os.environ if environ is None else environ
    smtp_host = environ["SMTP_HOST"]
    smtp_port = int(environ.get("SMTP_PORT", "587"))
    smtp_username = environ["SMTP_USERNAME"]
    smtp_password = environ["SMTP_PASSWORD"]
    recipient = environ["CHANGE_REPORT_TO"]

    message = EmailMessage()
    message["Subject"] = (
        "Bass directory update: validated changes"
        if report["summary"]["has_changes"]
        else "Bass directory update: no listing changes"
    )
    message["From"] = environ.get("ALERT_FROM") or smtp_username
    message["To"] = recipient
    message.set_content(format_report(report))
    with smtp_factory(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        refused = server.send_message(message)
    if refused:
        raise RuntimeError("SMTP refused one or more change-report recipients; delivery was not accepted")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send a validated Bass directory change report.")
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    send_report(report)
    print("Change report accepted by configured SMTP server.")


if __name__ == "__main__":
    main()
