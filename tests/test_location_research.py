import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from approve_location import approve, approved_commands
from directory_release import permanent_id, read_json, reconcile, venue_key, write_release
from research_locations import candidate_score, research
from coordinate_sources import camra_reference
from tests.test_directory_release import row


class ResearchAndApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.stage = Path(self.temp.name) / 'root', Path(self.temp.name) / 'stage'
        self.listing = row()
        self.identifier = permanent_id(venue_key(self.listing))
        self.listing['venue_id'] = self.identifier
        self.registry = {'schema_version': 1, 'venues': {self.identifier: dict(
            listing=self.listing, current_key=venue_key(self.listing), aliases=[venue_key(self.listing)])}}
        write_release(self.root, [self.listing], self.registry, {}, {'BS1 1AA': {'lat': 51.45, 'lng': -2.59}})
        (self.root / 'latest-bass-directory.pdf').write_bytes(b'test source')
        self.url = 'https://camra.org.uk/pubs/example-inn-123'
        self.event = {'issue': {'number': 5}, 'comment': {'id': 123, 'user': {'login': 'owner', 'type': 'User'},
                                                       'body': f'/verify-location {self.identifier} {self.url}',
                                                       'html_url': 'https://github.com/example/repo/issues/5#issuecomment-123'}}
        self.source = dict(lat=51.45, lng=-2.59, name='Example Inn, Bristol', postcode='BS1 1AA', address='1 High Street')

    def test_only_owner_human_commands_are_accepted(self):
        self.assertEqual(approved_commands(self.event, 'owner'), [(self.identifier, self.url)])
        for field, value in [('login', 'outsider'), ('type', 'Bot')]:
            event = copy.deepcopy(self.event)
            event['comment']['user'][field] = value
            with self.assertRaises(ValueError):
                approved_commands(event, 'owner')

    def test_host_injection_and_duplicate_venue_commands_are_rejected(self):
        for body in [self.event['comment']['body'].replace('camra.org.uk', 'evil.test'),
                     self.event['comment']['body'] + '\n' + self.event['comment']['body'],
                     self.event['comment']['body'] + '; echo unsafe']:
            event = copy.deepcopy(self.event)
            event['comment']['body'] = body
            with self.assertRaises(ValueError):
                approved_commands(event, 'owner')

    def test_approval_stages_fresh_evidence_and_retains_permanent_id(self):
        before = (self.root / 'venue-registry.json').read_bytes()
        with patch('approve_location.fetch_text'), patch('approve_location.parse_camra', return_value=self.source):
            approve(self.root, self.stage, self.event, 'owner')
        entry = read_json(self.stage / 'venue-registry.json')['venues'][self.identifier]
        self.assertEqual(entry['verified_pin']['evidence'][0]['review_url'], self.event['comment']['html_url'])
        self.assertEqual(read_json(self.stage / 'location-review.json')['unverified_count'], 0)
        self.assertEqual((self.root / 'venue-registry.json').read_bytes(), before)

    def test_distant_or_unrelated_sources_cannot_be_approved(self):
        for source in [dict(self.source, name='Unrelated Hotel, Bristol'), dict(self.source, lat=52.0)]:
            with patch('approve_location.fetch_text'), patch('approve_location.parse_camra', return_value=source):
                with self.assertRaises(ValueError):
                    approve(self.root, self.stage, self.event, 'owner')
        self.assertFalse((self.stage / 'directory-release.json').exists())

    def test_reviewed_postcode_correction_is_remembered_for_the_next_pdf(self):
        source = dict(self.source, postcode='BS1 1AB')
        with patch('approve_location.fetch_text'), patch('approve_location.parse_camra', return_value=source):
            approve(self.root, self.stage, self.event, 'owner')
        corrected = dict(self.listing, postcode='BS1 1AB')
        rows, registry, report = reconcile([corrected], read_json(self.stage / 'venue-registry.json'), {})
        self.assertEqual(report['errors'], [])
        self.assertEqual(rows[0]['venue_id'], self.identifier)
        self.assertNotIn('metadata_issue', registry['venues'][self.identifier]['verified_pin'])

    def test_owner_can_review_recorded_full_source_name_variant(self):
        source = dict(self.source, name='Example Inn Rose Lane, Bristol - Local Pub')
        self.registry['venues'][self.identifier]['location_research'] = {'proposals': [dict(source, url=self.url)]}
        write_release(self.root, [self.listing], self.registry, {}, {'BS1 1AA': {'lat': 51.45, 'lng': -2.59}})
        with patch('approve_location.fetch_text'), patch('approve_location.parse_camra', return_value=source):
            approve(self.root, self.stage, self.event, 'owner')
        self.assertEqual(read_json(self.stage / 'location-review.json')['unverified_count'], 0)

    def test_changed_candidate_coordinate_requires_research_again(self):
        self.registry['venues'][self.identifier]['location_research'] = {'proposals': [dict(self.source, url=self.url)]}
        write_release(self.root, [self.listing], self.registry, {}, {'BS1 1AA': {'lat': 51.45, 'lng': -2.59}})
        with patch('approve_location.fetch_text'), patch('approve_location.parse_camra', return_value=dict(self.source, lat=51.451)):
            with self.assertRaisesRegex(ValueError, 'changed since research'):
                approve(self.root, self.stage, self.event, 'owner')
        self.assertFalse((self.stage / 'directory-release.json').exists())

    def test_fsa_outage_does_not_prevent_independent_candidate_research(self):
        candidate = dict(self.source, url=self.url, town='Bristol')
        with patch('research_locations.resolve_new_pin', side_effect=TimeoutError()), patch('research_locations.discover', return_value=([candidate], {'complete': True})) as lookup, patch('research_locations.time.sleep'):
            report = research(self.root, self.stage)
        lookup.assert_called_once()
        self.assertTrue(report['valid'])
        self.assertEqual(report['automatically_verified'], [])
        entry = read_json(self.stage / 'venue-registry.json')['venues'][self.identifier]
        self.assertNotIn('verified_pin', entry)
        self.assertEqual(entry['location_research']['proposals'][0]['url'], self.url)

    def test_candidate_name_variants_are_suggestions_without_becoming_evidence(self):
        self.assertEqual(candidate_score('Black Cat', 'Black Cat Rose Lane'), 2)
        self.assertEqual(candidate_score('Example Inn', ''), 0)
        self.assertEqual(candidate_score('Red Lion', 'Blue Boar'), 0)

    def test_omitted_map_postcode_uses_named_page_address_instead_of_failing_match(self):
        map_venue = dict(Name='Example Inn', IncID=123, Latitude=51.45, Longitude=-2.59, Postcode=None)
        with patch('audit.fetch_camra.fetch', return_value={'venues': [map_venue]}), patch('coordinate_sources.camra_page', return_value=self.source):
            match = camra_reference(self.listing, {'lat': 51.45, 'lng': -2.59})
        self.assertIsNotNone(match)
        self.assertEqual(match['postcode'], 'BS1 1AA')
        self.assertEqual(match['address'], '1 High Street')

    def test_missing_map_postcode_cannot_override_an_actual_page_address_conflict(self):
        map_venue = dict(Name='Example Inn', IncID=123, Latitude=51.45, Longitude=-2.59, Postcode=None)
        with patch('audit.fetch_camra.fetch', return_value={'venues': [map_venue]}), patch('coordinate_sources.camra_page', return_value=dict(self.source, postcode='BS1 1AB')):
            self.assertIsNone(camra_reference(self.listing, {'lat': 51.45, 'lng': -2.59}))
