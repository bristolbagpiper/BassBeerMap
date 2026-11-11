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
