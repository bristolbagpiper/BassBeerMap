import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from google_conflicts import adjudicate_google_conflict
from google_listings import check_google
from location_review import build_review, notification_items
from notify_location_review import notify


class OwnerQuestionTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(venue_id='a',pub_name='White Horse',place_name='Town',postcode='AB1 2CD')
        self.pin = dict(lat=52.0,lng=-1.0,premises_review={'precision':'building'},evidence=[
            dict(source='camra',url='https://camra.org.uk/pubs/12345',checked_at='2026-01-01',
                 reference_coordinate=dict(lat=52,lng=-1),reference_address='42 High Street',reference_postcode='AB1 2CD')])
        self.entry = dict(current_key='a',listing=dict(self.row),verified_pin=copy.deepcopy(self.pin))
        self.registry = {'venues':{'a':self.entry}}
        self.ledger = {'records':{'a':{'display_precision':'venue'}}}
        self.panel = dict(url='https://www.google.com/maps/place/White+Horse/data=!1s0x1:0x2!3d52.007!4d-1',
                          h1=['White Horse'],namedBusinessPanel=True,category='Pub',
                          addressButtons=[{'label':'Address: 42 High Street, Town AB1 2CD'}])
        self.link = {'maps_url':'https://www.google.com/maps?cid=2'}
        self.smtp = MagicMock()
        self.server = self.smtp.return_value.__enter__.return_value
        self.server.send_message.return_value = {}
        self.env = dict(SMTP_HOST='test',SMTP_USERNAME='owner',SMTP_PASSWORD='test',ALERT_TO='owner@example.test')

    def review(self):
        return build_review([self.row], self.registry, self.ledger)

    def test_material_named_location_conflict_reaches_one_email_and_preserves_pin(self):
        def check(driver,row,pin,*args,observations):
            observations.append((None,self.panel))
            return 'pin_conflict',None
        with patch('selenium.webdriver.Chrome'), patch('google_listings.observe',side_effect=check):
            check_google([self.row],self.registry,[])
        self.assertEqual(self.entry['verified_pin'],self.pin)
        self.assertEqual(self.review()['actionable_count'],1)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)/'state.json'
            self.assertTrue(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env))
            self.panel['url'] = self.panel['url'].replace('52.007','52.00701')
            adjudicate_google_conflict(self.entry,self.row,'pin_conflict',[(None,self.panel)])
            self.assertFalse(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env))
            self.server.send_message.assert_called_once()
            self.assertIn(self.link['maps_url'],self.server.send_message.call_args.args[0].get_content())
            self.entry['acknowledged_google_conflict'] = {'snapshot':copy.deepcopy(self.entry['google_conflict'])}
            self.assertEqual(notification_items(self.review()),{})

    def test_identity_requires_a_tied_business_not_a_stray_search_result(self):
        page = dict(self.panel,h1=['Railway Tavern'],url=self.panel['url'].replace('52.007','52.00001'),
                    addressButtons=[{'label':'Address: 19 Low Street, Town AB1 2CD'}])
        adjudicate_google_conflict(self.entry,self.row,'identity_conflict',[(None,page)])
        self.assertFalse(notification_items(self.review()))
        self.entry['google_listing'] = dict(self.row, **self.link)
        adjudicate_google_conflict(self.entry,self.row,'identity_conflict',[(self.link,page)])
        self.assertIn('Railway Tavern',notification_items(self.review())['venue:a']['context'])
        adjudicate_google_conflict(self.entry,self.row,'ambiguous',[])
        self.assertTrue(notification_items(self.review()))
        adjudicate_google_conflict(self.entry,self.row,'named_listing',[])
        self.assertFalse(notification_items(self.review()))

    def test_small_offsets_and_ordinary_search_failures_are_quiet(self):
        for page in (dict(self.panel,url=self.panel['url'].replace('52.007','52.0012')),
                     dict(self.panel,namedBusinessPanel=False),dict(self.panel,category='Building')):
            adjudicate_google_conflict(self.entry,self.row,'pin_conflict',[(None,page)])
            self.assertFalse(notification_items(self.review()))
        for outcome in ('ambiguous','unavailable','provider_limit'):
            adjudicate_google_conflict(self.entry,self.row,outcome,[])
            self.assertFalse(notification_items(self.review()))

    def test_one_publication_mail_includes_question_and_receipt_survives_quiet_runs(self):
        adjudicate_google_conflict(self.entry,self.row,'pin_conflict',[(None,self.panel)])
        publication = {'commit':'a'*40,'edition':'November 2026'}
        with tempfile.TemporaryDirectory() as folder, patch('send_change_report_email.format_publication',create=True,
                    side_effect=lambda p,q: p['edition']+' '+q['venue:a']['context']):
            state = Path(folder)/'state.json'
            self.assertTrue(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env,publication=publication))
            self.assertFalse(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env))
            self.assertFalse(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env,publication=publication))
            self.server.send_message.assert_called_once()
            self.assertIn('November 2026',self.server.send_message.call_args.args[0]['Subject'])

    def test_uncertain_release_send_is_held_across_a_quiet_run_and_replay(self):
        release = {'commit':'b'*40,'pdf_revision':'a'*40,'edition':'November revision'}
        with tempfile.TemporaryDirectory() as folder, patch('send_change_report_email.format_publication',return_value='Published'):
            state = Path(folder)/'state.json'
            self.server.send_message.side_effect = OSError('SMTP connection lost during send')
            with self.assertRaisesRegex(RuntimeError,'uncertain'):
                notify(self.review(),state,smtp_factory=self.smtp,environ=self.env,publication=release)
            self.assertFalse(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env))
            self.assertFalse(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env,
                                    publication=dict(release,commit='c'*40)))
            self.assertFalse(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env,resolution='confirm_sent'))
            self.assertFalse(notify(self.review(),state,smtp_factory=self.smtp,environ=self.env,publication=release))
            self.server.send_message.assert_called_once()
