"""Email a concise, auditable summary of a validated directory refresh."""
import argparse
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def listing_label(row):
    return f"{row['pub_name']} — {row['place_name']} ({row['postcode']})"


def format_report(report):
    summary = report["summary"]
    lines = [
        "Bass directory update validated and ready to publish.",
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


def main():
    parser = argparse.ArgumentParser(description="Send a validated Bass directory change report.")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    recipient = os.environ["CHANGE_REPORT_TO"]
    message = EmailMessage()
    message["Subject"] = (
        "Bass directory update: validated changes"
        if report["summary"]["has_changes"]
        else "Bass directory update: no listing changes"
    )
    message["From"] = os.environ.get("ALERT_FROM", smtp_username)
    message["To"] = recipient
    message.set_content(format_report(report))
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(message)


if __name__ == "__main__":
    main()
