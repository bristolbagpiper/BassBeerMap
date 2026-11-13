import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from build_place_review_queue import build_queue
from directory_release import read_json
from hermes_google_browser import (_identity_ok, _point_from_url, _postcode_in_text, _reference_expectations, _resolve_page,
                                   _rotated, _valid_place_url, classify, run)
from hermes_google_review import build_status


ROOT = Path(__file__).resolve().parents[1]
PLACE_URL = 'https://www.google.com/maps/place/Example+Inn/@51.45,-2.59,17z/data=!3d51.45000!4d-2.59000'


class FakeBrowser:
    def __init__(self, dom):
        self.dom = dom
        self.navigations = []
        self.closed_task = None

    def browser_navigate(self, url, task_id=None):
        self.navigations.append((url, task_id))
        return {'success': True, 'url': url}

    def browser_snapshot(self, full=False, task_id=None):
        return {'success': True, 'snapshot': '- heading "Example Inn" [ref=e1]'}

    def browser_console(self, expression=None, task_id=None):
        return {'success': True, 'result': self.dom}

    def browser_click(self, ref, task_id=None):
        return {'success': True, 'clicked': ref}


class ConsentBrowser(FakeBrowser):
    def __init__(self, dom, visible_after_scroll=True):
        super().__init__(dom)
        self.snapshots = [
            {'success': True, 'snapshot': '- heading "Before you continue" [ref=e1]\n- button "Reject all" [ref=e10]'},
            {'success': True, 'snapshot': '- heading "Before you continue" [ref=e1]\n- button "Reject all" [ref=e11]'},
            {'success': True, 'snapshot': '- heading "Before you continue" [ref=e1]\n- button "Reject all" [ref=e12]'},
            {'success': True, 'snapshot': '- heading "Example Inn" [ref=e1]'},
        ]
        self.clicked = []
        self.scrolls = []
        self.dom_reads = [
            {'url': 'https://consent.google.com/', 'consentButtons': [{'inViewport': False}]},
            {'url': 'https://consent.google.com/', 'consentButtons': [{'inViewport': visible_after_scroll}]},
            dom,
        ]

    def browser_snapshot(self, full=False, task_id=None):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def browser_console(self, expression=None, task_id=None):
        return {'success': True, 'result': self.dom_reads.pop(0)}

    def browser_scroll(self, direction, task_id=None):
        self.scrolls.append(direction)
        return {'success': True, 'scrolled': direction}

    def browser_click(self, ref, task_id=None):
        self.clicked.append(ref)
        return {'success': True, 'clicked': ref}


class SearchResultBrowser(FakeBrowser):
    def __init__(self, search_dom, detail_dom, snapshot_lines):
        super().__init__(search_dom)
        self.dom_reads = [search_dom, detail_dom]
        self.snapshots = [snapshot_lines, '- heading "The Apiary at Castle Donington" [ref=e20]']
        self.clicked = []

    def browser_snapshot(self, full=False, task_id=None):
        return {'success': True, 'snapshot': self.snapshots.pop(0)}

    def browser_console(self, expression=None, task_id=None):
        return {'success': True, 'result': self.dom_reads.pop(0)}

    def browser_click(self, ref, task_id=None):
        self.clicked.append(ref)
        return {'success': True, 'clicked': ref}


class HermesGoogleBrowserTests(unittest.TestCase):
    def test_named_place_with_recorded_town_is_matched_without_accepting_another_town(self):
        dom = {'url': PLACE_URL, 'h1': ['The Apiary at Castle Donington'],
               'addressButtons': [{'label': 'Address: 38 Borough Street, DE74 2LA'}], 'body': ''}
        result = _resolve_page(FakeBrowser(dom), 'task', 'Apiary', {'DE742LA'}, set(),
                               place_name='Castle Donington')
        self.assertEqual(result['category'], 'identity_and_pin_consistent')
        wrong_town = dict(dom, h1=['The Apiary at Another Town'])
        rejected = _resolve_page(FakeBrowser(wrong_town), 'task', 'Apiary', {'DE742LA'}, set(),
                                 place_name='Castle Donington')
        self.assertEqual(rejected['category'], 'identity_conflict')

    def test_reviewed_trading_name_discards_camra_marketing_and_locality(self):
        entry = {'verified_pin': {'evidence': [{'reference_name': 'Alchemy at The Crown, Southam - Grade II Listed Pub',
                                              'reference_postcode': 'CV47 1PH'}]}}
        names, _ = _reference_expectations({'postcode': 'CV47 1PH'}, entry)
        self.assertEqual(names, {'Alchemy at The Crown'})
        self.assertTrue(_identity_ok({'pub_name': 'Crown'}, 'Alchemy at The Crown', names))
        self.assertFalse(_identity_ok({'pub_name': 'Crown'}, 'Alchemy at The Crown Annex', names))

    @classmethod
    def setUpClass(cls):
        bundle = read_json(ROOT / 'directory-release.json')
        registry = read_json(ROOT / 'venue-registry.json')
        queue = build_queue(bundle, registry, read_json(ROOT / 'audit/full-location-audit.json', {}))
        cls.queue = queue
        cls.item = queue[0]
        cls.entry = registry['venues'][cls.item['venue_id']]

    def test_dom_result_requires_one_named_heading_address_and_place_url(self):
        dom = {'url': PLACE_URL, 'h1': ['Example Inn'],
               'addressButtons': [{'label': 'Address: 1 Main Road, BS1 1AA'}], 'body': ''}
        browser = FakeBrowser(dom)
        result = _resolve_page(browser, 'task', 'Example Inn', {'BS11AA'}, set())
        self.assertEqual(result['category'], 'identity_and_pin_consistent')
        self.assertEqual(result['point'], {'lat': 51.45, 'lng': -2.59})
        with patch('hermes_google_browser.PAGE_DEADLINE', 0.01), patch('hermes_google_browser.time.sleep', lambda _: None):
            for field, value in [('h1', ['Example Inn', 'Second']), ('addressButtons', [])]:
                with self.subTest(field=field):
                    broken = dict(dom, **{field: value})
                    self.assertEqual(_resolve_page(FakeBrowser(broken), 'task', 'Example Inn',
                                                   {'BS11AA'}, set())['category'], 'ambiguous')
            search = dict(dom, url='https://www.google.com/maps/search/?api=1&q=Example')
            self.assertEqual(_resolve_page(FakeBrowser(search), 'task', 'Example Inn',
                                           {'BS11AA'}, set())['category'], 'unavailable')

    def test_pin_and_trading_conflicts_are_separate_categories(self):
        item = dict(self.item, current_pin={'lat': 51.0, 'lng': -2.0})
        page = {'category': 'identity_and_pin_consistent', 'result_url': PLACE_URL,
                'point': {'lat': 51.45, 'lng': -2.59}, 'body': 'Permanently closed'}
        self.assertEqual({x[0] for x in classify(item, self.entry, page)},
                         {'pin_conflict', 'trading_conflict'})

    def test_oldest_exact_bound_attempts_rotate_ahead_and_ties_keep_risk_order(self):
        queue = self.queue[:4]
        prior = {'observations': [
            dict(queue[0], attempted_at='2026-11-02T12:00:00Z'),
            dict(queue[1], attempted_at='2026-11-01T12:00:00Z'),
            dict(queue[2], attempted_at='2026-11-01T12:00:00+00:00'),
        ]}
        selected = _rotated(queue, prior, 4)
        self.assertEqual(selected[0]['venue_id'], queue[3]['venue_id'])  # never attempted
        self.assertEqual([x['venue_id'] for x in selected[1:3]],
                         [queue[1]['venue_id'], queue[2]['venue_id']])  # equal timestamp
        self.assertEqual(selected[3]['venue_id'], queue[0]['venue_id'])

    def test_due_closures_rechecked_without_displacing_ordinary_rotation(self):
        queue = self.queue[:5]
        now = datetime(2026, 11, 13, 8, tzinfo=timezone.utc)
        prior = {'observations': [
            dict(queue[0], attempted_at='2026-11-10T07:40:00Z', category='trading_conflict'),
            dict(queue[0], attempted_at='2026-11-10T07:40:00Z', category='pin_conflict'),
            dict(queue[0], attempted_at='2026-11-10T07:41:00Z', category='unavailable'),
            dict(queue[1], attempted_at='2026-11-09T08:00:00Z', category='trading_conflict'),
            dict(queue[1], attempted_at='2026-11-10T08:00:00Z', category='identity_and_pin_consistent'),
            dict(queue[2], attempted_at='2026-11-12T08:00:00Z', category='trading_conflict'),
            dict(queue[3], attempted_at='2026-11-09T08:00:00Z', category='trading_conflict',
                 binding_signature='old binding'),
        ]}
        self.assertEqual(_rotated(queue, prior, 2, now=now), [queue[0], queue[3]])
        self.assertEqual(_rotated(queue, prior, 1, now=now), [queue[3]])

    def test_only_active_evidence_names_and_postcodes_are_retained(self):
        entry = {'verified_pin': {'evidence': [
            {'reference_name': 'Rejected Alias', 'reference_postcode': 'ZZ1 1ZZ', 'disposition': 'rejected'},
            {'reference_name': 'Current Reference', 'reference_postcode': 'BS1 1AA'},
        ]}}
        names, postcodes = _reference_expectations(dict(self.item, postcode='BS2 2BB'), entry)
        self.assertEqual(names, {'Current Reference'})
        self.assertEqual(postcodes, {'BS11AA', 'BS22BB'})

    def test_result_postcode_match_requires_full_code_boundaries(self):
        self.assertTrue(_postcode_in_text('BS11AA', 'Address, BS1 1AA, Bristol'))
        self.assertFalse(_postcode_in_text('BS11AA', 'Address, XBS1 1AA9, Bristol'))

    def test_named_urls_and_markers_are_strict(self):
        self.assertTrue(_valid_place_url(PLACE_URL))
        self.assertFalse(_valid_place_url('https://evil.example/maps/place/Example/'))
        self.assertFalse(_valid_place_url('https://www.google.com/maps/place/@51.4,-2.5,15z'))
        self.assertIsNone(_point_from_url(PLACE_URL + '!3d51.46!4d-2.58'))
        self.assertIsNone(_point_from_url(PLACE_URL.replace('!3d51.45000', '!3d999')))

    def test_approximate_saved_pin_never_yields_consistency(self):
        item = dict(self.item, precision='postcode', current_pin_source='postcode_fallback')
        page = {'category': 'identity_and_pin_consistent', 'result_url': PLACE_URL,
                'point': {'lat': 51.45, 'lng': -2.59}, 'body': ''}
        self.assertEqual(classify(item, self.entry, page), [('ambiguous', PLACE_URL)])

    def test_consent_scrolls_confirms_viewport_refreshes_ref_then_clicks_once(self):
        dom = {'url': PLACE_URL, 'h1': ['Example Inn'],
               'addressButtons': [{'label': 'Address: BS1 1AA'}], 'body': ''}
        browser = ConsentBrowser(dom)
        result = _resolve_page(browser, 'task', 'Example Inn', {'BS11AA'}, set(),
                               'https://www.google.com/maps/search/?api=1&query=Example')
        self.assertEqual(result['category'], 'identity_and_pin_consistent')
        self.assertEqual(browser.scrolls, ['down'])
        self.assertEqual(browser.clicked, ['e12'])

    def test_consent_remains_blocked_after_one_native_click_and_stops(self):
        dom = {'url': 'https://consent.google.com/', 'consentButtons': [{'inViewport': True}]}
        browser = ConsentBrowser(dom)
        browser.snapshots[-1] = {'success': True,
                                 'snapshot': '- heading "Before you continue" [ref=e1]\n- button "Reject all" [ref=e12]'}
        with patch('hermes_google_browser.PAGE_DEADLINE', 0.02), patch('hermes_google_browser.time.sleep', lambda _: None):
            result = _resolve_page(browser, 'task', 'Example Inn', {'BS11AA'}, set(),
                                   'https://www.google.com/maps/search/?api=1&query=Example')
        self.assertEqual(result, {'category': 'unavailable', 'stop': True,
                                  'detail': 'Google consent screen stayed open'})
        self.assertEqual(browser.clicked, ['e12'])
        self.assertEqual(browser.scrolls, ['down'])

    def test_cleared_consent_followed_by_results_is_ordinary_ambiguity(self):
        search_url = 'https://www.google.com/maps/search/?api=1&query=Black+Lion'
        candidates = [
            {'href': f'https://www.google.com/maps/place/Black+Lion+Inn/data?cid={n}',
             'text': 'Black Lion Inn'} for n in range(3)
        ]
        dom = {'url': search_url, 'h1': ['Results'], 'addressButtons': [],
               'placeLinks': candidates, 'body': 'Results'}
        browser = ConsentBrowser(dom)
        browser.snapshots[-1] = {'success': True, 'snapshot': (
            '- heading "Results" [ref=e1]\n'
            '- link "Black Lion Inn" [ref=e20]\n'
            '- link "Black Lion Inn" [ref=e21]\n'
            '- link "Black Lion Inn" [ref=e22]')}
        browser.dom_reads[-1] = dom
        result = _resolve_page(browser, 'task', 'Black Lion', {'ST137SP'}, set(),
                               search_url, place_name='Butterton')
        self.assertEqual(result['category'], 'ambiguous')
        self.assertFalse(result['stop'])
        self.assertEqual(browser.clicked, ['e12'])  # consent only; no branch was selected

    def test_unique_named_result_uses_native_ref_then_checks_detail_postcode(self):
        search_url = 'https://www.google.com/maps/search/?api=1&query=Apiary'
        result_url = 'https://www.google.com/maps/place/The+Apiary+at+Castle+Donington/data?cid=1'
        result_dom = {'url': search_url, 'h1': ['Results'], 'addressButtons': [],
                      'placeLinks': [{'href': result_url, 'text': 'The Apiary at Castle Donington'}],
                      'body': 'Results'}
        detail_dom = {'url': PLACE_URL, 'h1': ['The Apiary at Castle Donington'],
                      'addressButtons': [{'label': 'Address: 38 Borough Street, DE74 2LA'}], 'body': ''}
        browser = SearchResultBrowser(result_dom, detail_dom,
            '- heading "Results" [ref=e1]\n- link "The Apiary at Castle Donington" [ref=e44]')
        result = _resolve_page(browser, 'task', 'Apiary', {'DE742LA'}, set(), search_url,
                               place_name='Castle Donington')
        self.assertEqual(result['category'], 'identity_and_pin_consistent')
        self.assertEqual(browser.clicked, ['e44'])

    def test_two_matching_named_results_remain_ambiguous_without_click(self):
        search_url = 'https://www.google.com/maps/search/?api=1&query=Black+Lion'
        result_dom = {'url': search_url, 'h1': ['Results'], 'addressButtons': [],
                      'placeLinks': [
                          {'href': 'https://www.google.com/maps/place/Black+Lion+Inn/data?cid=1',
                           'text': 'Black Lion Inn'},
                          {'href': 'https://www.google.com/maps/place/Black+Lion+Inn/data?cid=2',
                           'text': 'Black Lion Inn'}], 'body': 'Results'}
        browser = SearchResultBrowser(result_dom, {},
            '- heading "Results" [ref=e1]\n- link "Black Lion Inn" [ref=e30]\n'
            '- link "Black Lion Inn" [ref=e31]')
        result = _resolve_page(browser, 'task', 'Black Lion', {'ST137SP'}, set(), search_url,
                               place_name='Butterton')
        self.assertEqual(result['category'], 'ambiguous')
        self.assertFalse(result['stop'])
        self.assertEqual(browser.clicked, [])

    def test_ambiguous_results_flow_through_the_receipt_consumer(self):
        dom = {'url': 'https://www.google.com/maps/search/?api=1&query=Example',
               'h1': ['Results'], 'addressButtons': [], 'placeLinks': [], 'body': 'Results'}
        browser = FakeBrowser(dom)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'receipts.json'
            result = run(ROOT, target, limit=1, venue_ids=[self.item['venue_id']], browser=browser,
                         cleanup=lambda task: None,
                         clock=iter(['2026-11-03T12:00:00Z', '2026-11-03T12:00:01Z']).__next__)
            self.assertFalse(result['stopped'])
            document = json.loads(target.read_text(encoding='utf-8'))
            status = build_status(ROOT, document,
                                  now=datetime.fromisoformat('2026-11-03T12:01:00+00:00'))
            self.assertEqual(status['counts']['current'], 0)
            self.assertIn('ambiguous', status['findings'][0]['reason'])

    def test_deadline_cannot_report_a_complete_unattempted_batch(self):
        browser = FakeBrowser({})
        with tempfile.TemporaryDirectory() as temp, patch('hermes_google_browser.TOTAL_DEADLINE', 0):
            target = Path(temp) / 'receipts.json'
            result = run(ROOT, target, limit=1, venue_ids=[self.item['venue_id']],
                         browser=browser, cleanup=lambda task: None)
            self.assertEqual(result, {'attempted': 0, 'stopped': True,
                                      'selected': 1, 'checked': 0, 'ambiguous': 0, 'unavailable': 0,
                                      'detail': 'Browser batch deadline reached before all selected venues were attempted'})
            self.assertEqual(browser.navigations, [])

    def test_one_page_has_one_check_time_for_all_conflict_categories(self):
        dom = {'url': PLACE_URL, 'h1': [self.item['pub_name']],
               'addressButtons': [{'label': 'Address: ' + self.item['postcode']}],
               'body': 'Temporarily closed'}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'receipts.json'
            run(ROOT, target, limit=1, venue_ids=[self.item['venue_id']], browser=FakeBrowser(dom),
                cleanup=lambda task: None,
                clock=iter(['2026-11-03T12:00:00Z', '2026-11-03T12:00:01Z']).__next__)
            document = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual({o['category'] for o in document['observations']}, {'pin_conflict', 'trading_conflict'})
            self.assertEqual({o['checked_at'] for o in document['observations']}, {'2026-11-03T12:00:01Z'})

    def test_run_uses_native_mock_and_appends_minimal_receipt(self):
        bundle = read_json(ROOT / 'directory-release.json')
        postcode = self.item['postcode']
        # Identity conflict receipts still require a named place URL; no Google text is stored.
        dom = {'url': PLACE_URL, 'h1': ['A Different Business'],
               'addressButtons': [{'label': 'Address: ' + postcode}], 'body': ''}
        browser = FakeBrowser(dom)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'receipts.json'
            target.write_text(json.dumps({'schema_version': 1, 'observations': []}), encoding='utf-8')
            result = run(ROOT, target, limit=1, venue_ids=[self.item['venue_id']], browser=browser,
                         cleanup=lambda task: setattr(browser, 'closed_task', task),
                         clock=iter(['2026-11-03T12:00:00Z', '2026-11-03T12:00:01Z']).__next__)
            document = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(result, {'attempted': 1, 'stopped': False, 'selected': 1,
                                      'checked': 1, 'ambiguous': 0, 'unavailable': 0})
            self.assertEqual(document['observations'][0]['category'], 'identity_conflict')
            self.assertEqual(document['observations'][0]['source_url'], '')
            self.assertEqual(set(document['observations'][0]), {
                'schema_version', 'provider', 'venue_id', 'identity_signature', 'point_signature',
                'binding_signature', 'category', 'attempted_at', 'checked_at', 'result_url', 'source_url'})
            self.assertTrue(browser.closed_task.startswith('hermes-google-'))
            self.assertEqual(len(browser.navigations), 1)
            status = build_status(ROOT, document,
                                  now=datetime.fromisoformat('2026-11-03T12:01:00+00:00'))
            self.assertEqual(status['counts']['attempted_venue_count'], 1)

    def test_temporary_navigation_error_retries_once_and_records_one_observation(self):
        browser = FakeBrowser({'url': PLACE_URL, 'h1': [self.item['pub_name']],
                               'addressButtons': [{'label': 'Address: ' + self.item['postcode']}], 'body': ''})
        pauses = []
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'receipts.json'
            with patch.object(browser, 'browser_navigate', side_effect=[
                {'success': False, 'error': 'net::ERR_CONNECTION_RESET'}, {'success': True},
            ]) as calls:
                result = run(ROOT, target, limit=1, venue_ids=[self.item['venue_id']], browser=browser,
                             sleeper=pauses.append)
            observations = read_json(target)['observations']
        self.assertEqual(result, {'attempted': 1, 'stopped': False, 'selected': 1,
                                  'checked': 1, 'ambiguous': 0, 'unavailable': 0})
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(calls.call_args_list[0], calls.call_args_list[1])
        self.assertEqual(pauses, [2])
        self.assertEqual(len(observations), 1)
        self.assertIsNotNone(observations[0]['checked_at'])

    def test_navigation_challenge_stops_without_retry_and_records_only_safe_detail(self):
        browser = FakeBrowser({})
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'receipts.json'
            with patch.object(browser, 'browser_navigate', return_value={
                'success': False, 'error': 'Command timed out after 30 seconds',
                'bot_detection_warning': 'Page text to keep out of the receipt',
            }) as calls:
                result = run(ROOT, target, limit=1, venue_ids=[self.item['venue_id']], browser=browser)
            observation = read_json(target)['observations'][0]
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result, {'attempted': 1, 'stopped': True,
                                  'selected': 1, 'checked': 0, 'ambiguous': 0, 'unavailable': 1,
                                  'detail': 'Google presented an automated-traffic challenge'})
        self.assertEqual(observation['category'], 'unavailable')
        self.assertIsNone(observation['checked_at'])

    def test_invalid_existing_receipts_fail_before_browser_navigation(self):
        browser = FakeBrowser({'url': PLACE_URL, 'h1': ['Example Inn'],
                               'addressButtons': [{'label': 'Address: ' + self.item['postcode']}], 'body': ''})
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'receipts.json'
            target.write_text('{"schema_version":1,"observations":[],"extra":true}', encoding='utf-8')
            with self.assertRaises(ValueError):
                run(ROOT, target, limit=1, venue_ids=[self.item['venue_id']], browser=browser,
                    cleanup=lambda _: None)
            self.assertFalse(browser.navigations)
            self.assertFalse(target.with_name(target.name + '.lock').exists())


if __name__ == '__main__':
    unittest.main()
