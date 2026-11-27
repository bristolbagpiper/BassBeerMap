import json
import unittest
from datetime import date
from unittest.mock import patch
from selenium.common.exceptions import WebDriverException

from google_listings import check_google, identify, public_link, same_premises, targets


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

    def test_literal_place_id_upgrade_opens_the_new_visitor_url(self):
        place_id = 'ChIJabcdefghijklmnopqrstuvwxyz'
        page = dict(self.page, url=self.page['url'] + '!19s' + place_id + '!')
        _, listing = identify(self.row, self.pin, page)
        _, original = identify(self.row, self.pin, self.page)
        entry = {'verified_pin': self.pin, 'google_listing': original}
        with patch('selenium.webdriver.Chrome'), patch('google_listings.observe',
                side_effect=[('named_listing', listing), ('named_listing', listing)]) as observe:
            check_google([self.row], {'venues': {'one': entry}}, [])
        self.assertEqual(observe.call_args.args[-1], {'place_id': place_id})
        self.assertEqual(public_link(self.row, entry['google_listing']), {'place_id': place_id})

    def test_named_search_that_links_to_a_building_is_withheld(self):
        registry = {'venues': {'one': {'verified_pin': self.pin}}}
        link = dict(self.row, maps_url='https://www.google.com/maps?cid=123')
        with patch('selenium.webdriver.Chrome'), patch('google_listings.observe',
                side_effect=[('named_listing', link), ('ambiguous', None)]) as observe:
            self.assertEqual(check_google([self.row], registry, []), {'one':'destination_conflict'})
        self.assertEqual(observe.call_count, 2)
        self.assertNotIn('google_listing', registry['venues']['one'])

    def test_two_directory_localities_may_share_one_corroborated_business(self):
        pin = dict(self.pin, evidence=[{'source':'camra','url':'https://camra.org.uk/pubs/123054'}])
        a = {'listing':self.row, 'verified_pin':pin}
        b = {'listing':dict(self.row, place_name='Town: District'), 'verified_pin':dict(pin)}
        self.assertTrue(same_premises(a, b))
        b['verified_pin']['evidence'] = [{'source':'camra','url':'https://camra.org.uk/pubs/999'}]
        self.assertFalse(same_premises(a, b))

    def test_confirmed_trading_name_is_only_used_for_the_existing_listing(self):
        alias = 'The Crown Coaching Inn'
        entry = {'verified_pin': self.pin, 'google_listing': dict(self.row, matched_name=alias,
                 maps_url='https://www.google.com/maps?cid=171')}
        with patch('selenium.webdriver.Chrome'), patch('google_listings.observe',
                side_effect=[('identity_conflict', None), ('ambiguous', None)]) as observe:
            check_google([self.row], {'venues': {'one': entry}}, [])
        self.assertEqual(observe.call_args_list[0].args[2]['confirmed_google_name'], alias)
        self.assertNotIn('confirmed_google_name', observe.call_args_list[1].args[2])
        self.assertNotIn('google_listing', entry)

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

    def test_verified_unchanged_listing_is_skipped_even_with_approximate_pin(self):
        pin = dict(self.pin, premises_review={'precision':'reference'})
        entry = {'verified_pin':pin, 'google_listing':dict(self.row, place_id='ChIJ123456789'),
                 'google_check':{'date':'2026-11-20','binding':['The Crown','Town','AB1 2CD',52,-1]}}
        self.assertEqual(targets([self.row], {'venues':{'one':entry}}, [self.row], date(2026,11,27)), [])

    def test_prior_registry_detects_material_identity_address_name_and_pin_authority(self):
        today = date(2026,11,27)
        old = {'venues':{'one':{'listing':dict(self.row), 'verified_pin':dict(self.pin)}}}
        cases = [
            (dict(self.row, pub_name='Crown Inn'), dict(self.pin)),
            (dict(self.row), dict(self.pin, alternate_name='Crown Inn')),
            (dict(self.row), dict(self.pin, metadata_issue={'listed':'AB1 2CD','reference':'AB1 2CE',
                 'source_url':'https://example.org/address','use_reference_for_directions':True})),
            (dict(self.row), dict(self.pin, evidence=[{'source':'survey','url':'https://example.org/pin',
                 'checked_at':'2026-11-20','reference_coordinate':{'lat':52,'lng':-1}}])),
        ]
        for changed_row, changed_pin in cases:
            current = {'venues':{'one':{'listing':dict(changed_row), 'verified_pin':changed_pin,
                                        'google_listing':dict(changed_row, place_id='ChIJ123456789'),
                                        'google_check':{'date':'2026-11-26','binding':[changed_row[k] for k in
                                         ('pub_name','place_name','postcode')] + [52,-1]}}}}
            self.assertEqual(len(targets([changed_row], current, [changed_row], today, old)), 1)

    def test_prior_registry_ignores_evidence_timestamp_refresh(self):
        old_pin = dict(self.pin, evidence=[{'source':'survey','url':'https://example.org/pin',
            'checked_at':'2026-10-01','reference_coordinate':{'lat':52,'lng':-1}}])
        new_pin = dict(old_pin, evidence=[dict(old_pin['evidence'][0], checked_at='2026-11-20')])
        previous = {'venues':{'one':{'listing':dict(self.row), 'verified_pin':old_pin}}}
        current = {'venues':{'one':{'listing':dict(self.row), 'verified_pin':new_pin,
            'google_listing':dict(self.row, place_id='ChIJ123456789'),
            'google_check':{'date':'2026-11-20','binding':['The Crown','Town','AB1 2CD',52,-1]}}}}
        self.assertEqual(targets([self.row], current, [self.row], date(2026,11,27), previous), [])

    def test_priority_queue_is_unbounded_before_twenty_ordinary_targets(self):
        rows, venues = [], {}
        for i in range(55):
            row = dict(self.row, venue_id=str(i), pub_name=f'Pub {i}')
            rows.append(row)
            venues[str(i)] = {'verified_pin':dict(self.pin), 'google_check':{'date':'2026-01-01',
                'binding':[row['pub_name'],'Town','AB1 2CD',52,-1]}}
        previous_rows = [dict(row) for row in rows]
        for row in rows[:25]:
            previous_rows[int(row['venue_id'])]['pub_name'] += ' Old'
        queue = targets(rows, {'venues':venues}, previous_rows, date(2026,11,27))
        self.assertEqual(len(queue), 45)
        self.assertTrue(all(item[0] == 0 for item in queue[:25]))
        self.assertTrue(all(item[0] == 1 for item in queue[25:]))

    def test_pending_only_selects_only_durable_markers(self):
        pending = {'verified_pin':self.pin, 'google_priority_pending':True}
        discretionary = {'verified_pin':self.pin, 'google_check':{'date':'2026-01-01'}}
        registry = {'venues':{'one':pending, 'two':discretionary}}
        rows = [self.row, dict(self.row, venue_id='two')]
        self.assertEqual([item[3] for item in targets(rows, registry, rows, date(2026,11,27), pending_only=True)], ['one'])

    def test_provider_limit_mid_import_resumes_only_the_unfinished_priority_rows(self):
        rows = [dict(self.row, venue_id=f'p{i:02}', pub_name=f'New Pub {i}') for i in range(35)]
        previous = rows[28:]
        registry = {'venues': {row['venue_id']: {'verified_pin': dict(self.pin)} for row in rows}}
        with patch('selenium.webdriver.Chrome'), patch('google_listings.observe',
                side_effect=[('ambiguous',None)] * 22 + [('provider_limit',None)]):
            first = check_google(rows,registry,previous)
        self.assertEqual(len(first),23)
        restored = json.loads(json.dumps(registry))
        pending = {key for key, entry in restored['venues'].items() if entry.get('google_priority_pending')}
        self.assertEqual(pending,{row['venue_id'] for row in rows[22:28]})
        with patch('selenium.webdriver.Chrome'), patch('google_listings.observe',return_value=('ambiguous',None)):
            second = check_google(rows,restored,rows,pending_only=True)
        self.assertEqual(set(second),pending)
        self.assertFalse(any(entry.get('google_priority_pending') for entry in restored['venues'].values()))
        with patch('selenium.webdriver.Chrome') as chrome:
            self.assertEqual(check_google(rows,restored,rows,pending_only=True),{})
            chrome.assert_not_called()

    def test_browser_startup_failure_keeps_priority_marker_for_resume(self):
        changed = dict(self.row, pub_name='Crown Inn')
        entry = {'verified_pin':self.pin, 'google_listing':dict(changed, place_id='ChIJ123456789')}
        previous = [dict(self.row)]
        registry = {'venues':{'one':entry}}
        with patch('selenium.webdriver.Chrome', side_effect=WebDriverException('startup failed')):
            result = check_google([changed], registry, previous, today=date(2026,11,27))
        self.assertTrue(entry['google_priority_pending'])
        self.assertIn('browser', result)
        self.assertEqual([item[3] for item in targets([changed], registry, previous, date(2026,11,27), pending_only=True)], ['one'])
