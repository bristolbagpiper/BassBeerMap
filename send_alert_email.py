import argparse
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a workflow alert email.")
    parser.add_argument("--subject", required=True, help="Email subject.")
    parser.add_argument("--report", required=True, help="Path to the validation report JSON.")
    args = parser.parse_args()

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    alert_to = os.environ["ALERT_TO"]
    alert_from = os.environ.get("ALERT_FROM", smtp_username)
    github_server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    github_repo = os.environ.get("GITHUB_REPOSITORY", "")
    github_run_id = os.environ.get("GITHUB_RUN_ID", "")

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    run_url = f"{github_server}/{github_repo}/actions/runs/{github_run_id}" if github_repo and github_run_id else ""

    body_lines = [
        "The Bass directory workflow rejected the latest PDF/CSV refresh.",
        "",
        "Validation errors:",
    ]
    body_lines.extend(f"- {error}" for error in report.get("errors", []))
    if run_url:
        body_lines.extend(["", f"Workflow run: {run_url}"])

    message = EmailMessage()
    message["Subject"] = args.subject
    message["From"] = alert_from
    message["To"] = alert_to
    message.set_content("\n".join(body_lines))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(message)


if __name__ == "__main__":
    main()
