import unittest
from datetime import date
from unittest.mock import patch

from google_listings import check_google, identify, public_link, targets


class ListingTests(unittest.TestCase):
    row = dict(venue_id='one', pub_name='The Crown', place_name='Town', postcode='AB1 2CD')
    pin = dict(lat=52, lng=-1, premises_review={'precision':'building'})
    page = dict(url='https://www.google.com/maps/place/The+Crown/data=!4m2!1s0x1:0xab!3d52!4d-1',
                namedBusinessPanel=True, category='Pub', h1=['Crown'], addressButtons=[{'label':'Address: High St, AB1 2CD'}])

    def test_named_panel_at_matching_address_produces_business_identifier(self):
        outcome, link = identify(self.row, self.pin, self.page)
        self.assertEqual(outcome, 'named_listing')
        self.assertEqual(public_link(self.row, link), {'maps_url':'https://www.google.com/maps?cid=171'})
        self.assertIsNone(public_link(dict(self.row, postcode='AB1 2CE'), link))

    def test_conflicts_and_map_camera_are_insufficient(self):
        for changes in ({'h1':['Crown Cottage']}, {'addressButtons':[{'label':'AB1 2CDE'}]},
                        {'namedBusinessPanel':False}, {'category':'Building'}, {'url':self.page['url'].replace('!3d52','!3d53')},
                        {'url':'https://www.google.com/maps/place/Crown/@52,-1,17z'}):
            with self.subTest(changes=changes):
                self.assertIsNone(identify(self.row, self.pin, dict(self.page, **changes))[1])

    def test_named_search_that_links_to_a_building_is_withheld(self):
        registry = {'venues': {'one': {'verified_pin': self.pin}}}
        link = dict(self.row, maps_url='https://www.google.com/maps?cid=123')
        with patch('selenium.webdriver.Chrome'), patch('google_listings.observe',
                side_effect=[('named_listing', link), ('ambiguous', None)]) as observe:
            self.assertEqual(check_google([self.row], registry, []), {'one':'destination_conflict'})
        self.assertEqual(observe.call_count, 2)
        self.assertNotIn('google_listing', registry['venues']['one'])

    def test_selection_uses_changed_identity_and_rechecks_unresolved_on_cooldown(self):
        entry = {'verified_pin':self.pin, 'google_listing':dict(self.row, place_id='ChIJ123456789')}
        registry = {'venues':{'one':entry}}
        today = date(2026,11,27)
        self.assertEqual(targets([self.row], registry, [self.row], today), [])
        self.assertEqual(len(targets([self.row], registry, [], today)), 1)
        del entry['google_listing']
        entry['google_check'] = {'date':'2026-11-20','binding':['The Crown','Town','AB1 2CD',52,-1]}
        self.assertEqual(targets([self.row], registry, [self.row], today), [])
        entry['verified_pin'] = dict(self.pin, lng=-1.1)
        self.assertEqual(len(targets([self.row], registry, [self.row], today)), 1)
