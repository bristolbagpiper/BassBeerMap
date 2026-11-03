import unittest
from unittest.mock import patch

from coordinate_sources import resolve_new_pin
from research_locations import discover
from tests.test_directory_release import row


class CamraPrimaryResolutionTests(unittest.TestCase):
    centre = {'lat': 51.45, 'lng': -2.59}

    def test_invalid_supporting_fsa_coordinate_cannot_corrupt_camra_pin(self):
        reference = dict(self.centre, url='https://camra.org.uk/pubs/123',
                         name='Example Inn', postcode='BS1 1AA', address='1 High Street')
        diagnostics = {}
        with patch('coordinate_sources.search', return_value=[]), patch(
                'coordinate_sources.choose_match', return_value={'lat': float('nan'), 'lng': -2.59}), patch(
                'coordinate_sources.camra_reference', return_value=reference):
            result = resolve_new_pin(row(), diagnostics, centre=self.centre)
        self.assertEqual(result['lat'], self.centre['lat'])
        self.assertEqual(diagnostics['sources']['fsa']['status'], 'invalid_coordinates')
        self.assertEqual(len(result['evidence']), 1)

    def test_exact_camra_match_approves_when_fsa_is_ambiguous(self):
        reference = {'lat': 51.4502, 'lng': -2.5901, 'url': 'https://camra.org.uk/pubs/123',
                     'name': 'Example Inn', 'postcode': 'BS1 1AA', 'address': '1 High Street'}
        diagnostics = {}
        with patch('coordinate_sources.search', return_value=[]), patch(
                'coordinate_sources.camra_reference', return_value=reference):
            result = resolve_new_pin(row(), diagnostics, centre=self.centre)
        self.assertIsNotNone(result)
        self.assertEqual((result['lat'], result['lng']), (reference['lat'], reference['lng']))
        self.assertEqual(result['evidence'][0]['source'], 'camra')

    def test_no_camra_match_never_approves_even_with_fsa_point(self):
        diagnostics = {}
        def unmatched(listing, centre, report):
            report['sources']['camra'] = {'status': 'unmatched'}
            return None
        with patch('coordinate_sources.search', return_value=[]), patch(
                'coordinate_sources.choose_match', return_value={'lat': 51.45, 'lng': -2.59, 'fhrs_id': 42}), patch(
                'coordinate_sources.camra_reference', side_effect=unmatched):
            self.assertIsNone(resolve_new_pin(row(), diagnostics, centre=self.centre))
        self.assertEqual(diagnostics['sources']['camra']['status'], 'unmatched')

    def test_ambiguous_camra_match_never_approves_even_with_fsa_point(self):
        diagnostics = {}
        def ambiguous(listing, centre, report):
            report['sources']['camra'] = {'status': 'ambiguous'}
            return None
        with patch('coordinate_sources.search', return_value=[]), patch(
                'coordinate_sources.choose_match', return_value={'lat': 51.45, 'lng': -2.59, 'fhrs_id': 42}), patch(
                'coordinate_sources.camra_reference', side_effect=ambiguous):
            self.assertIsNone(resolve_new_pin(row(), diagnostics, centre=self.centre))
        self.assertEqual(diagnostics['sources']['camra']['status'], 'ambiguous')


class CamraCandidateDiscoveryTests(unittest.TestCase):
    def test_name_variant_is_only_a_review_proposal_and_page_is_checked(self):
        listing = row('Black Cat')
        venue = dict(Name='Black Cat Rose Lane', IncID=123, Latitude=51.45, Longitude=-2.59,
                     Postcode='BS1 1AA', Street='Rose Lane', Town='Bristol')
        page = dict(lat=51.45, lng=-2.59, name='Black Cat Rose Lane', postcode='BS1 1AA',
                    address='Rose Lane', locality='Bristol')
        with patch('audit.fetch_camra.fetch', return_value={'venues': [venue], 'total': 1}), patch(
                'research_locations.camra_page', return_value=page) as named_page:
            proposals, coverage = discover(listing, {'lat': 51.45, 'lng': -2.59})
        named_page.assert_called_once_with('https://camra.org.uk/pubs/123')
        self.assertTrue(coverage['complete'])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]['source_status'], 'named_page_checked')
        self.assertEqual(proposals[0]['score'], 2)

    def test_incomplete_map_coverage_is_reported_not_silently_called_complete(self):
        venue = dict(Name='Example Inn', IncID=123, Latitude=51.45, Longitude=-2.59,
                     Postcode='BS1 1AA', Street='1 High Street')
        with patch('audit.fetch_camra.fetch', return_value={'venues': [venue], 'total': 200}), patch(
                'research_locations.camra_page', return_value=dict(lat=51.45, lng=-2.59, name='Example Inn',
                                                                    postcode='BS1 1AA', address='1 High Street')):
            _, coverage = discover(row(), {'lat': 51.45, 'lng': -2.59})
        self.assertFalse(coverage['complete'])


if __name__ == '__main__':
    unittest.main()
