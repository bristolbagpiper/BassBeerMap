import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from location_review import format_report
from notify_location_review import notify


class ReviewReportSectionTests(unittest.TestCase):
    def test_active_source_outage_is_warning_while_identity_and_trading_are_actions(self):
        records = {
            'source': dict(pub_name='Source Pub', place_name='Town', postcode='BS1', active=True,
                           unverified=False, status='review_needed', first_seen='today', reasons=['Verification source unavailable: timeout'],
                           review_category='source_warning', manual_action_required=False, next_action='Retry source'),
            'identity': dict(pub_name='Identity Pub', place_name='Town', postcode='BS2', active=True,
                             unverified=True, status='unverified', first_seen='today', reasons=['Venue name changed'],
                             review_category='identity_check', manual_action_required=True, next_action='Check business identity'),
            'trading': dict(pub_name='Trading Pub', place_name='Town', postcode='BS3', active=True,
                            unverified=False, status='review_needed', first_seen='today', reasons=['Trading status needs review'],
                            review_category='trading_check', manual_action_required=True, next_action='Check trading status'),
        }
        text = format_report(dict(records=records, active_count=3, actionable_count=2,
                                  source_warning_count=1, unverified_count=1))
        action_section = text.split('## Action required', 1)[1].split('## Source warnings', 1)[0]
        source_section = text.split('## Source warnings', 1)[1].split('## Information', 1)[0]
        self.assertIn('Identity Pub', action_section)
        self.assertIn('Trading Pub', action_section)
        self.assertNotIn('Source Pub', action_section)
        self.assertIn('Source Pub', source_section)
        self.assertIn('Retry source', source_section)
        self.assertIn('Action required: 2', text)

    def test_google_operational_issue_is_not_counted_as_pub_review(self):
        report = dict(records={}, active_count=0, actionable_count=0, source_warning_count=0,
                      unverified_count=0, google_issues=['Google comparison is not configured'], google_count=1,
                      google_findings=[])
        text = format_report(report)
        self.assertIn('Google operational status: Google comparison is not configured', text)
        self.assertIn('Google pub actions: 0', text)

    def test_subject_separates_source_warning_from_actionable_reviews(self):
        report = dict(records={}, active_count=3, actionable_count=2, source_warning_count=1,
                      unverified_count=1, google_count=2, google_actionable_count=1,
                      google_source_warning_count=1, google_findings=[])
        env = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test', ALERT_TO='test@example.test')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True):
            smtp = MagicMock()
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(notify(report, Path(directory) / 'state.json', smtp_factory=smtp))
            subject = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]['Subject']
            self.assertIn('2 actions', subject)
            self.assertIn('1 source warnings', subject)
            self.assertIn('1 Google pub actions', subject)
            self.assertIn('1 Google source warnings', subject)
            self.assertNotIn('Google comparison reviews', subject)


if __name__ == '__main__':
    unittest.main()
