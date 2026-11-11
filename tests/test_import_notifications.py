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

    def test_workflow_keeps_validation_publication_and_delivery_separate(self):
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "update-csv-from-pdf.yml"
        text = workflow.read_text(encoding="utf-8")

        self.assertIn("id: candidate_validation", text)
        self.assertIn("id: publication", text)
        self.assertIn("state=published", text)
        self.assertIn("state=unchanged", text)
        self.assertIn("id: change_report_email", text)
        self.assertIn("state=not_configured", text)
        self.assertIn("state=suppressed_no_changes", text)
        self.assertIn("state=accepted", text)
        self.assertIn("steps.change_report_email.outcome != 'failure'", text)
        self.assertIn("Report import email delivery failure", text)
        self.assertIn("continue-on-error: true", text)
        self.assertIn("steps.change_report_email.outcome == 'failure'", text)
        self.assertNotIn("|| exit 0", text)


if __name__ == "__main__":
    unittest.main()
