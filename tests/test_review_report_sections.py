import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from location_review import fingerprint, format_notification, format_report, notification_items, with_trading_notice_reviews
from notify_location_review import notify


class ReviewReportSectionTests(unittest.TestCase):
    def test_due_public_notice_changes_the_consolidated_report_once(self):
        report = dict(records={}, active_count=0)
        bundle = {'rows': [{'venue_id': 'venue', 'pub_name': 'Pub', 'place_name': 'Town'}]}
        notices = {'notices': [{'venue_id': 'venue', 'review_by': '2026-11-20'}]}
        active = with_trading_notice_reviews(report, notices, bundle, date(2026, 11, 20))
        due = with_trading_notice_reviews(report, notices, bundle, date(2026, 11, 21))
        still_due = with_trading_notice_reviews(report, notices, bundle, date(2026, 11, 22))
        self.assertEqual(active['trading_notice_reviews'], [])
        self.assertIn('Pub — Town (venue); review due 2026-11-20.', format_report(due))
        self.assertNotEqual(fingerprint(active), fingerprint(due))
        self.assertEqual(fingerprint(due), fingerprint(still_due))

    def test_owner_can_see_the_conflicting_postcodes_and_original_notice(self):
        report = {'records': {'a': dict(pub_name='Example', active=True, manual_action_required=True,
                  review_category='address_metadata', postcode='AB1 2CD',
                  reference={'listed': 'AB1 2CD', 'reference': 'AB1 2EF', 'source_url': 'https://example.org/licence'})}}
        listing = {'venue_id':'b', 'pub_name':'Other pub', 'place_name':'Town'}
        notice = dict(listing, review_by='2026-01-01', text='The kitchen is closed.', source_url='https://example.org/operator')
        due = with_trading_notice_reviews(report, {'notices':[notice]}, {'rows':[listing]}, date(2026,1,2))
        message = format_notification(notification_items(due))
        for detail in ('AB1 2CD', 'AB1 2EF', 'licence', notice['text'], notice['source_url']):
            self.assertIn(detail, message)
        hidden = with_trading_notice_reviews(report, {'notices':[notice], 'manual_hides':['b']},
                                            {'rows':[listing]}, date(2026,1,2))
        self.assertNotIn('notice:b', notification_items(hidden))

    def test_due_notice_shares_receipts_between_daily_review_and_publication(self):
        listing = {'venue_id': 'venue', 'pub_name': 'Example Pub', 'place_name': 'Town'}
        notice = dict(venue_id='venue', review_by='2026-11-20', text='Check local opening hours.')
        publication = dict(commit='a' * 40, edition='November revision')
        env = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test',
                   ALERT_TO='test@example.test')

        def report_for(day):
            return with_trading_notice_reviews({'records': {}}, {'notices': [notice]},
                                               {'rows': [listing]}, date(2026, 11, day))

        for publication_first in (False, True):
            with self.subTest(publication_first=publication_first), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / 'state.json'
                smtp = MagicMock()
                send = smtp.return_value.__enter__.return_value.send_message
                send.return_value = {}

                def deliver(day, revision=None):
                    return notify(report_for(day), state, smtp_factory=smtp, environ=env, publication=revision)

                self.assertFalse(deliver(20))
                if publication_first:
                    self.assertTrue(deliver(21, publication))
                else:
                    self.assertTrue(deliver(21))
                    self.assertFalse(deliver(21))
                    self.assertTrue(deliver(22, publication))
                self.assertFalse(deliver(22))
                self.assertFalse(deliver(22, publication))
                self.assertFalse(deliver(23))

                subjects = [call.args[0]['Subject'] for call in send.call_args_list]
                expected = ['BassBeerMap: November revision published']
                if not publication_first:
                    expected.insert(0, 'BassBeerMap: 1 changed review question')
                self.assertEqual(subjects, expected)
                self.assertIn('Outstanding owner decisions', send.call_args.args[0].get_content())
                self.assertIn(notice['text'], send.call_args.args[0].get_content())

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

    def test_email_contains_only_changed_owner_question_not_diagnostics(self):
        report = dict(records={'venue-a': dict(active=True, manual_action_required=True,
                      pub_name='Example Pub', place_name='Town', postcode='BS1 1AA',
                      review_category='trading_check', next_action='Check trading status',
                      reasons=['Trading status needs review'])}, active_count=1,
                      google_count=2, google_issues=['Google comparison is stale'],
                      health_issues=['Live release unreachable'])
        env = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test', ALERT_TO='test@example.test')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True):
            smtp = MagicMock()
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(notify(report, Path(directory) / 'state.json', smtp_factory=smtp))
            subject = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]['Subject']
            body = smtp.return_value.__enter__.return_value.send_message.call_args.args[0].get_content()
            self.assertEqual(subject, 'BassBeerMap: 1 changed review question')
            self.assertIn('Can you confirm the current trading/Bass status for Example Pub?', body)
            self.assertNotIn('Google', body)
            self.assertNotIn('unreachable', body)
            self.assertNotIn('Last checked', body)

    def test_only_changed_venue_is_emailed_and_cleared_item_can_recur(self):
        def item(name, reason, action='Corroborate the current pin'):
            return dict(active=True, manual_action_required=True, pub_name=name, place_name='Town',
                        postcode='BS1 1AA', review_category='needs_verification',
                        next_action=action, reasons=[reason])
        report = dict(records={'a': item('Alpha', 'Missing evidence'), 'b': item('Beta', 'Approximate pin')})
        env = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test', ALERT_TO='test@example.test')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True):
            state = Path(directory) / 'state.json'
            smtp = MagicMock()
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(notify(report, state, smtp_factory=smtp))
            changed = dict(report, records={'a': item('Alpha', 'Missing evidence'),
                                            'b': item('Beta', 'New independent source disagrees',
                                                      'Check the newly identified entrance')})
            self.assertTrue(notify(changed, state, smtp_factory=smtp))
            body = smtp.return_value.__enter__.return_value.send_message.call_args.args[0].get_content()
            self.assertIn('## Beta', body)
            self.assertNotIn('## Alpha', body)
            cleared = dict(changed, records={'a': item('Alpha', 'Missing evidence')})
            self.assertFalse(notify(cleared, state, smtp_factory=smtp))
            self.assertTrue(notify(changed, state, smtp_factory=smtp))
            self.assertEqual(smtp.call_count, 3)


if __name__ == '__main__':
    unittest.main()
