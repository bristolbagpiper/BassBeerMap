import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from coordinate_sources import camra_reference, resolve_new_pin
from directory_release import build_ledger, permanent_id, read_json, venue_key
from location_review import build_review, fingerprint, format_report
from notify_location_review import notify
from tests.test_directory_release import row, pin


class LocationTrackingTests(unittest.TestCase):
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

    def test_dates_do_not_duplicate_alerts_but_new_failure_reasons_do(self):
        listing, registry, identifier = self.fixture()
        review = build_review([listing], registry, build_ledger([listing], registry))
        later = copy.deepcopy(review)
        later['records'][identifier]['last_checked'] = '2026-10-09'
        self.assertEqual(fingerprint(review), fingerprint(later))
        later['records'][identifier]['reasons'] = ['CAMRA coordinate disagrees']
        self.assertNotEqual(fingerprint(review), fingerprint(later))

    def test_fsa_failure_does_not_claim_camra_has_no_listing(self):
        diagnostics = {}
        with patch('coordinate_sources.search', return_value=[]), patch('coordinate_sources.camra_reference') as camra:
            self.assertIsNone(resolve_new_pin(row(), diagnostics))
        camra.assert_not_called()
        self.assertEqual(diagnostics['sources']['camra']['status'], 'not_checked')
        self.assertEqual(diagnostics['sources']['fsa']['status'], 'no_postcode_match')

    def test_camra_match_with_disagreeing_coordinate_is_tracked(self):
        diagnostics = {}
        candidate = dict(Name='Example Inn', Postcode='BS1 1AA', Latitude=51.46, Longitude=-2.59,
                         IncID=123, PremisesStatus='O')
        with patch('audit.fetch_camra.fetch', return_value={'venues': [candidate]}):
            self.assertIsNone(camra_reference(row(), {'lat': 51.45, 'lng': -2.59}, diagnostics))
        self.assertEqual(diagnostics['sources']['camra']['status'], 'coordinate_disagreement')
        self.assertIn('CAMRA venue found', diagnostics['reason'])

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
            self.assertEqual(read_json(state)['email_fingerprint'], fingerprint(review))
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
