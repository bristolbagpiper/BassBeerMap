import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from coordinate_sources import camra_reference, resolve_new_pin
from directory_release import build_ledger, permanent_id, read_json, venue_key
from location_review import (build_review, fingerprint, format_report, legacy_notification_fingerprint,
                             load_health_status, notification_items, with_health)
from notify_location_review import notify
from tests.test_directory_release import row, pin


class LocationTrackingTests(unittest.TestCase):
    def test_documented_postcode_difference_reopens_when_location_or_reference_changes(self):
        from location_review import postcode_issue_reviewed
        venue = row()
        checked = pin()
        urls = ['https://ratings.food.gov.uk/business/123', 'https://example.org/contact']
        issue = {'listed': venue['postcode'], 'reference': 'BS1 1XY', 'source_url': urls[0]}
        checked['premises_review'] = {'venue_key': venue_key(venue), 'point': {k: checked[k] for k in ('lat', 'lng')},
                                      'precision': 'building', 'checked_at': '2026-01-01', 'basis': 'Numbered pub building',
                                      'building_url': 'https://www.openstreetmap.org/way/123', 'geometry_feature': 'way/123',
                                      'sources': urls + ['https://www.openstreetmap.org/way/123'],
                                      'postal_address': dict(issue, sources=urls)}
        self.assertTrue(postcode_issue_reviewed(issue, checked, venue['pub_name'], venue))
        self.assertFalse(postcode_issue_reviewed(dict(issue, reference='BS1 1ZZ'), checked, venue['pub_name'], venue))
        self.assertFalse(postcode_issue_reviewed(issue, dict(checked, lat=51), venue['pub_name'], venue))

    def test_health_and_pipeline_failures_do_not_change_owner_question_notifications(self):
        empty = dict(records={}, active_count=0, unverified_count=0)
        healthy = dict(schema_version=1, issues=[], run_id='10', checked_at='2026-10-31T10:00:00Z')
        self.assertEqual(with_health(empty, healthy)['health_checked_at'], healthy['checked_at'])
        failed = dict(schema_version=1, issues=['Live release unreachable: TimeoutError'], run_id='11')
        actionable = dict(records={'venue-1': dict(active=True, manual_action_required=True,
            pub_name='Example Inn', place_name='Bristol', postcode='BS1 1AA', review_category='needs_verification',
            next_action='Corroborate missing evidence', reasons=['Location has not yet been independently verified'])},
            active_count=1)
        settings = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test', ALERT_TO='test@example.test')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, settings, clear=True):
            state = Path(directory) / 'delivery.json'
            smtp = MagicMock()
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertFalse(notify(with_health(empty, None), state, failure_url='https://example.test/fail', smtp_factory=smtp))
            self.assertFalse(notify(with_health(empty, healthy), state, smtp_factory=smtp))
            self.assertTrue(notify(with_health(actionable, failed), state, failure_url='https://example.test/fail', smtp_factory=smtp))
            body = smtp.return_value.__enter__.return_value.send_message.call_args.args[0].get_content()
            self.assertIn('Can you verify the mapped entrance or site for Example Inn?', body)
            self.assertNotIn('Live release unreachable', body)
            self.assertNotIn('TimeoutError', body)
            noisy = copy.deepcopy(actionable)
            noisy['records']['venue-1']['reasons'] = ['Location not verified', 'Verification source unavailable: timeout']
            self.assertFalse(notify(with_health(noisy, failed), state, smtp_factory=smtp))
            later = dict(failed, run_id='12', checked_at='2026-11-01', run_url='https://example.test/12')
            self.assertFalse(notify(with_health(actionable, later), state, failure_url='', smtp_factory=smtp))
            self.assertFalse(notify(with_health(empty, healthy), state, smtp_factory=smtp))
            self.assertEqual(smtp.call_count, 1)
        self.assertEqual(empty, dict(records={}, active_count=0, unverified_count=0))

    def test_corrupt_or_unknown_health_status_fails_visibly(self):
        empty = dict(records={}, active_count=0, unverified_count=0)
        for status in ([], {}, {'schema_version': 2, 'issues': []},
                       {'schema_version': 1, 'issues': 'failure'}, {'schema_version': 1, 'issues': [None]}):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'Invalid map health'):
                with_health(empty, status)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            self.assertIsNone(load_health_status(path))
            for content in ('null', 'broken json'):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_health_status(path)

    def fixture(self):
        listing = row()
        identifier = permanent_id(venue_key(listing))
        listing['venue_id'] = identifier
        registry = {'venues': {identifier: dict(current_key=venue_key(listing), listing=listing,
                                              verification_attempt={'checked_at': '2026-10-02', 'reason': 'No FSA name match',
                                                                    'sources': {'fsa': {'status': 'no_name_match'}, 'camra': {'status': 'not_checked'}}})}}
        return listing, registry, identifier

    def test_failures_resolutions_and_removals_retain_history(self):
        listing, registry, identifier = self.fixture()
        review = build_review([listing], registry, build_ledger([listing], registry))
        registry['location_review'] = review
        self.assertEqual(review['unverified_count'], 1)
        self.assertIn('CAMRA: not_checked', format_report(review))
        registry['venues'][identifier]['verified_pin'] = pin()
        resolved = build_review([listing], registry, build_ledger([listing], registry))
        self.assertEqual(resolved['active_count'], 0)
        self.assertEqual(resolved['records'][identifier]['history'][-1]['status'], 'resolved')
        registry['location_review'] = resolved
        removed = build_review([], registry, {'records': {}})
        self.assertEqual(removed['records'][identifier]['history'][-1]['status'], 'removed')
        self.assertEqual(len(removed['records'][identifier]['history']), 3)

    def test_dates_and_failure_reasons_do_not_change_the_owner_question(self):
        listing, registry, identifier = self.fixture()
        review = build_review([listing], registry, build_ledger([listing], registry))
        later = copy.deepcopy(review)
        later['records'][identifier]['last_checked'] = '2026-10-09'
        self.assertEqual(fingerprint(review), fingerprint(later))
        later['records'][identifier]['reasons'] = ['CAMRA coordinate disagrees']
        self.assertEqual(fingerprint(review), fingerprint(later))
        later['records'][identifier]['review_category'] = 'identity_check'
        later['records'][identifier]['next_action'] = 'Check whether the named business changed'
        self.assertNotEqual(fingerprint(review), fingerprint(later))

    def test_fsa_failure_still_checks_camra_from_postcode_centre(self):
        diagnostics = {}
        centre = {'lat': 51.45, 'lng': -2.59}
        reference = {'lat': 51.4501, 'lng': -2.59, 'url': 'https://camra.org.uk/pubs/123',
                     'name': 'Example Inn', 'postcode': 'BS1 1AA', 'address': '1 High Street'}
        with patch('coordinate_sources.search', side_effect=TimeoutError()), patch(
                'coordinate_sources.camra_reference', return_value=reference) as camra:
            result = resolve_new_pin(row(), diagnostics, centre=centre)
        self.assertEqual(result['lat'], reference['lat'])
        camra.assert_called_once_with(row(), centre, diagnostics)
        self.assertEqual(diagnostics['sources']['fsa']['status'], 'unavailable')

    def test_camra_coordinate_disagreement_with_fsa_is_warning_not_veto(self):
        centre = {'lat': 51.45, 'lng': -2.59}
        reference = {'lat': 51.4505, 'lng': -2.59, 'url': 'https://camra.org.uk/pubs/123',
                     'name': 'Example Inn', 'postcode': 'BS1 1AA', 'address': '1 High Street'}
        fsa = [{'BusinessName': 'Example Inn', 'PostCode': 'BS1 1AA', 'Latitude': 51.46,
                'Longitude': -2.59, 'FHRSID': 42}]
        diagnostics = {}
        with patch('coordinate_sources.search', return_value=fsa), patch(
                'coordinate_sources.choose_match', return_value={'lat': 51.46, 'lng': -2.59, 'fhrs_id': 42}), patch(
                'coordinate_sources.camra_reference', return_value=reference):
            result = resolve_new_pin(row(), diagnostics, centre=centre)
        self.assertIsNotNone(result)
        self.assertEqual((result['lat'], result['lng']), (reference['lat'], reference['lng']))
        self.assertEqual(diagnostics['sources']['fsa']['status'], 'coordinate_disagreement')
        self.assertGreater(diagnostics['sources']['fsa']['difference_metres'], 50)

    def test_camra_match_with_disagreeing_coordinate_is_tracked(self):
        diagnostics = {}
        candidate = dict(Name='Example Inn', Postcode='BS1 1AA', Latitude=51.46, Longitude=-2.59,
                         IncID=123, PremisesStatus='O')
        page = dict(lat=51.46, lng=-2.59, name='Example Inn', postcode='BS1 1AA',
                    address='1 High Street', locality='Bristol')
        with patch('audit.fetch_camra.fetch', return_value={'venues': [candidate]}), patch(
                'coordinate_sources.camra_page', return_value=page) as named_page:
            match = camra_reference(row(), {'lat': 51.45, 'lng': -2.59}, diagnostics)
        named_page.assert_called_once_with('https://camra.org.uk/pubs/123')
        self.assertEqual(match['lat'], 51.46)
        self.assertEqual(diagnostics['sources']['camra']['status'], 'matched')

    def test_two_exact_named_page_matches_are_ambiguous(self):
        centre = {'lat': 51.45, 'lng': -2.59}
        venues = [dict(Name='Example Inn', Postcode='BS1 1AA', Latitude=51.45, Longitude=-2.59,
                       IncID=123, PremisesStatus='O'),
                  dict(Name='Example Inn', Postcode='BS1 1AA', Latitude=51.4501, Longitude=-2.59,
                       IncID=124, PremisesStatus='O')]
        page = dict(lat=51.45, lng=-2.59, name='Example Inn', postcode='BS1 1AA',
                    address='1 High Street', locality='Bristol')
        diagnostics = {}
        with patch('audit.fetch_camra.fetch', return_value={'venues': venues, 'total': 2}), patch(
                'coordinate_sources.camra_page', return_value=page) as named_page:
            self.assertIsNone(camra_reference(row(), centre, diagnostics))
        self.assertEqual(named_page.call_count, 2)
        self.assertEqual(diagnostics['sources']['camra']['status'], 'ambiguous')

    def test_map_still_incomplete_after_five_bounded_queries(self):
        centre = {'lat': 51.45, 'lng': -2.59}
        venue = dict(Name='Example Inn', Postcode='BS1 1AA', Latitude=51.45, Longitude=-2.59,
                     IncID=123, PremisesStatus='O')
        page = dict(lat=51.45, lng=-2.59, name='Example Inn', postcode='BS1 1AA',
                    address='1 High Street', locality='Bristol')
        diagnostics = {}
        with patch('audit.fetch_camra.fetch', return_value={'venues': [venue], 'total': 100}) as fetch, patch(
                'coordinate_sources.camra_page', return_value=page):
            self.assertIsNone(camra_reference(row(), centre, diagnostics))
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(diagnostics['sources']['camra']['status'], 'incomplete')

    def test_email_failure_is_not_acknowledged_and_success_is_deduplicated(self):
        listing, registry, identifier = self.fixture()
        review = build_review([listing], registry, build_ledger([listing], registry))
        settings = dict(SMTP_HOST='smtp.example.test', SMTP_USERNAME='test', SMTP_PASSWORD='test', ALERT_TO='test@example.test')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, settings):
            state = Path(directory) / 'delivery.json'
            failing = MagicMock()
            failing.return_value.__enter__.return_value.send_message.side_effect = TimeoutError()
            with self.assertRaises(TimeoutError):
                notify(review, state, smtp_factory=failing)
            self.assertFalse(state.exists())
            working = MagicMock()
            working.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(notify(review, state, smtp_factory=working))
            self.assertEqual(read_json(state)['items']['venue:' + identifier]['pub_name'], review['records'][identifier]['pub_name'])
            self.assertFalse(notify(review, state, smtp_factory=working))
            self.assertEqual(working.call_count, 1)

    def test_missing_email_configuration_is_visible_and_retryable(self):
        listing, registry, _ = self.fixture()
        review = build_review([listing], registry, build_ledger([listing], registry))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            state = Path(directory) / 'delivery.json'
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                notify(review, state)
            self.assertFalse(state.exists())

    def test_real_committed_snapshot_and_legacy_ack_replay_stays_quiet_for_diagnostics(self):
        root = Path(__file__).resolve().parents[1]
        report = read_json(root / 'location-review.json')
        legacy = read_json(root / 'location-alert-state.json')
        self.assertIn('email_fingerprint', legacy)
        legacy.pop('items', None)
        legacy.pop('schema_version', None)
        replay = dict(report, health_issues=['Live release unreachable: TimeoutError'],
                      google_issues=['Google comparison status is missing or stale'],
                      google_findings=[], google_count=2, google_actionable_count=0,
                      pipeline_failure=True)
        legacy.pop('items', None)  # Reproduce the pre-migration aggregate-only receipt.
        legacy['email_fingerprint'] = legacy_notification_fingerprint(replay)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(__import__('json').dumps(legacy), encoding='utf-8')
            smtp = MagicMock()
            self.assertFalse(notify(replay, state, failure_url='https://example.test/rejected-import', smtp_factory=smtp))
            self.assertEqual(smtp.call_count, 0)
            self.assertEqual(read_json(state)['items'], notification_items(replay))

    def test_seeded_historical_receipt_stays_quiet_and_preserves_original_receipt(self):
        root = Path(__file__).resolve().parents[1]
        current = read_json(root / 'location-review.json')
        bundle = read_json(root / 'directory-release.json')
        from location_review import with_trading_notice_reviews
        current = with_trading_notice_reviews(current, read_json(root / 'trading-notices.json'), bundle)
        state_snapshot = read_json(root / 'location-alert-state.json')
        self.assertEqual(len(state_snapshot['items']), 12)
        current_items = notification_items(current)
        self.assertEqual(len(current_items), 0)
        self.assertFalse(set(state_snapshot['items']) & set(current_items))
        smtp = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(__import__('json').dumps(state_snapshot), encoding='utf-8')
            self.assertFalse(notify(current, state, smtp_factory=smtp))
            self.assertEqual(smtp.call_count, 0)
            migrated = read_json(state)
            self.assertEqual(migrated['items'], {})
            self.assertEqual(migrated['emailed_at'], state_snapshot['emailed_at'])
            self.assertEqual(migrated['legacy_ack']['email_fingerprint'], state_snapshot['email_fingerprint'])

    def test_owner_postcode_review_resolves_only_exact_approved_case(self):
        listing, registry, identifier = self.fixture()
        entry = registry['venues'][identifier]
        entry['verified_pin'] = pin()
        issue = dict(listed=listing['postcode'], reference='BS1 1AB', source_url='https://camra.org.uk/pubs/123')
        issue['owner_review'] = dict(checked_at='2026-10-13', reviewed_by='bristolbagpiper', owner_instruction='yes',
                                    pub_name=listing['pub_name'], listed=issue['listed'], reference=issue['reference'],
                                    source_url=issue['source_url'], approved_coordinate={'lat':51.45,'lng':-2.59})
        entry['verified_pin']['metadata_issue'] = issue
        def active():
            return build_review([listing], registry, build_ledger([listing], registry))['active_count']
        self.assertEqual(active(), 0)
        from full_location_audit import classify
        check = classify(dict(status='coordinate_agreement', listing={'pub_name':listing['pub_name']}), entry['verified_pin'])
        self.assertFalse(check['actionable'])
        self.assertTrue(check['warnings'])
        before = copy.deepcopy(issue)
        for key, value in [('listed','BS1 1AC'), ('reference','BS1 1AD'), ('source_url','https://camra.org.uk/pubs/456')]:
            issue[key] = value
            self.assertEqual(active(), 1)
            issue.update(copy.deepcopy(before))
        entry['verified_pin']['lat'] = 51.4501
        self.assertEqual(active(), 1)
        entry['verified_pin']['lat'] = 51.45
        listing['pub_name'] = 'Renamed Pub'
        from directory_release import venue_key
        entry['current_key'] = venue_key(listing)
        self.assertEqual(active(), 1)

    def test_building_approval_does_not_resolve_trading_status_conflict(self):
        listing, registry, identifier = self.fixture()
        entry = registry['venues'][identifier]
        entry['verified_pin'] = pin()
        entry['location_approvals'] = [{'owner_instruction':'yes near enough'}]
        entry['trading_status_review'] = dict(status='needs_review', reason='CAMRA reports closed; operator advertises opening hours',
                                            evidence=[dict(claim='Closed long term',url='https://camra.org.uk/pubs/123')])
        ledger = build_ledger([listing], registry)
        before = copy.deepcopy(ledger)
        report = build_review([listing], registry, ledger)
        self.assertEqual(report['active_count'], 1)
        self.assertEqual(report['unverified_count'], 0)
        self.assertIn('Trading status needs review', report['records'][identifier]['reasons'][0])
        self.assertIn('https://camra.org.uk/pubs/123', format_report(report))
        self.assertEqual(ledger, before)
        registry['location_review'] = report
        entry['trading_status_review']['status'] = 'resolved'
        resolved = build_review([listing], registry, ledger)
        self.assertEqual(resolved['active_count'], 0)
        self.assertEqual(resolved['records'][identifier]['status'], 'resolved')
        self.assertNotEqual(fingerprint(report), fingerprint(resolved))

    def test_closed_review_is_scoped_to_current_point_and_postcodes(self):
        from apply_closed_reviews import apply_decisions
        listing, registry, identifier = self.fixture()
        entry = registry['venues'][identifier]
        entry['verified_pin'] = pin()
        entry['verified_pin']['metadata_issue'] = dict(listed=listing['postcode'], reference='BS1 1AB', source_url='https://camra.org.uk/pubs/123')
        decision = dict(venue_id=identifier, pub_name=listing['pub_name'], listed=listing['postcode'],
                        reference='BS1 1AB', source_url='https://camra.org.uk/pubs/123',
                        approved_coordinate={'lat':51.45, 'lng':-2.59}, checked_at='2026-10-31',
                        reviewed_by='bristolbagpiper', owner_instruction='close review')
        self.assertTrue(apply_decisions(registry, {'postcode_reviews': [decision]}))
        self.assertEqual(build_review([listing], registry, build_ledger([listing], registry))['active_count'], 0)
        self.assertFalse(apply_decisions(registry, {'postcode_reviews': [decision]}))
        entry['verified_pin']['metadata_issue']['reference'] = 'BS1 1AC'
        self.assertFalse(apply_decisions(registry, {'postcode_reviews': [decision]}))
        self.assertEqual(build_review([listing], registry, build_ledger([listing], registry))['active_count'], 1)

    def test_trading_acknowledgement_reopens_when_evidence_changes(self):
        from apply_closed_reviews import apply_decisions
        listing, registry, identifier = self.fixture()
        entry = registry['venues'][identifier]
        entry['verified_pin'] = pin()
        entry['trading_status_review'] = dict(status='needs_review', reason='conflicting information', evidence=[])
        decision = {'venue_id': identifier, 'snapshot': copy.deepcopy(entry['trading_status_review']),
                    'reviewed_by': 'bristolbagpiper', 'checked_at': '2026-10-31'}
        self.assertTrue(apply_decisions(registry, {'trading_reviews': [decision]}))
        self.assertEqual(build_review([listing], registry, build_ledger([listing], registry))['active_count'], 0)
        entry['trading_status_review']['reason'] = 'new conflicting information'
        self.assertFalse(apply_decisions(registry, {'trading_reviews': [decision]}))
        self.assertEqual(build_review([listing], registry, build_ledger([listing], registry))['active_count'], 1)

