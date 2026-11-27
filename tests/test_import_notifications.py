import unittest
from unittest.mock import MagicMock

from send_change_report_email import send_report


REPORT = {
    "summary": {
        "has_changes": True,
        "previous_listing_count": 1,
        "candidate_listing_count": 2,
        "added_count": 1,
        "removed_count": 0,
        "updated_count": 0,
    },
    "added": [{"pub_name": "Example Inn", "place_name": "Bristol", "postcode": "BS1 1AA"}],
    "removed": [],
    "updated": [],
}

ENV = {
    "SMTP_HOST": "smtp.example.test",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "sender@example.test",
    "SMTP_PASSWORD": "test-password",
    "CHANGE_REPORT_TO": "recipient@example.test",
    "ALERT_FROM": "Bass Map <sender@example.test>",
}


class ImportNotificationTests(unittest.TestCase):
    def test_success_requires_smtp_acceptance_and_sends_expected_report(self):
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value.send_message.return_value = {}

        send_report(REPORT, smtp_factory=smtp, environ=ENV)

        smtp.assert_called_once_with("smtp.example.test", 587, timeout=30)
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_called_once_with()
        server.login.assert_called_once_with("sender@example.test", "test-password")
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "recipient@example.test")
        self.assertEqual(message["From"], "Bass Map <sender@example.test>")
        self.assertIn("Example Inn — Bristol (BS1 1AA)", message.get_content())
        self.assertIn("committed to the repository", message.get_content())
        self.assertIn("checked separately", message.get_content())

    def test_refused_recipient_is_a_delivery_failure(self):
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value.send_message.return_value = {
            "recipient@example.test": (550, b"mailbox unavailable")
        }

        with self.assertRaisesRegex(RuntimeError, "refused one or more.*not accepted"):
            send_report(REPORT, smtp_factory=smtp, environ=ENV)

    def test_smtp_transport_errors_propagate_for_workflow_failure_reporting(self):
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value.send_message.side_effect = TimeoutError("timed out")

        with self.assertRaises(TimeoutError):
            send_report(REPORT, smtp_factory=smtp, environ=ENV)

    def test_workflow_dispatches_verified_pdf_publication_to_consolidated_notifier(self):
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "update-csv-from-pdf.yml"
        text = workflow.read_text(encoding="utf-8")

        self.assertIn("id: candidate_validation", text)
        self.assertIn("id: publication", text)
        self.assertIn("state=published", text)
        self.assertIn("state=unchanged", text)
        self.assertIn("notify-published-pdf:", text)
        self.assertIn("needs.update-csv.outputs.new_pdf == 'true'", text)
        self.assertIn("workflow_id: 'location-alerts.yml'", text)
        self.assertIn("inputs: { publication_commit, previous_commit, published_on }", text)
        self.assertIn("steps.pages_release.outcome == 'success'", text)
        self.assertIn("steps.source_fingerprint.outputs.unchanged == 'true'", text)
        self.assertNotIn("email_change_report", text)
        self.assertNotIn("send_change_report_email.py", text)
        self.assertIn('cron: "17 6 * * *"', text)
        self.assertIn("if: failure() && steps.canonical_event.outputs.current != 'false'", text)
        self.assertNotIn('github.event_name != "workflow_dispatch"', text)
        self.assertIn("const title = 'BassBeerMap update rejected'", text)
        self.assertIn("Report dispatch failure", text)
        self.assertIn("publication_commit", text)
        self.assertIn("Re-run the notify-published-pdf job", text)


if __name__ == "__main__":
    unittest.main()
