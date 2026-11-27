import csv
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from build_change_report import published_report
from send_change_report_email import format_publication


def snapshot_files(rows, *, pdf=b'%PDF-A', edition='November 2026 Bass directory',
                   street='High Street', precision='venue', actionable=True,
                   evidence_checked_at='2026-11-20', lat=51.4, lng=-2.6,
                   effective_postcode=None, visitor_hint=None):
    row_bytes = io.StringIO()
    fields = list(rows[0]) if rows else ['venue_id', 'pub_name', 'place_name', 'postcode', 'pg', 'dispense']
    writer = csv.DictWriter(row_bytes, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    locations, records = {}, {}
    for row in rows:
        identifier = row['venue_id']
        key = '|'.join(row[k].casefold() for k in ('pub_name', 'place_name', 'postcode'))
        locations[identifier] = {
            'lat': lat, 'lng': lng, 'precision': precision,
            'premises_precision': 'building' if precision == 'venue' else 'postcode',
            'directions_address': {'street': street, 'postcode': effective_postcode or row['postcode'], 'checked_at': '2026-11-20'},
            'visitor_hint': visitor_hint,
            'google_listing': {'maps_url': 'https://www.google.com/maps?cid=123'} if precision == 'venue' else None,
        }
        records[key] = {'venue_id': identifier, 'status': 'verified_venue' if precision == 'venue' else 'postcode_approximate',
                        'evidence': [{'source': 'CAMRA', 'url': 'https://camra.org.uk/pubs/123',
                                      'checked_at': evidence_checked_at, 'reference_address': street,
                                      'reference_postcode': row['postcode']},
                                     {'source': 'Google Maps', 'url': 'https://www.google.com/maps?cid=123',
                                      'checked_at': '2026-11-20'},
                                     {'source': 'old CAMRA record', 'url': 'https://camra.org.uk/pubs/old',
                                      'checked_at': '2020-01-01'},
                                     {'source': 'superseded', 'url': 'https://example.org/old',
                                      'checked_at': '2026-11-20', 'disposition': 'superseded'}]
                        if precision == 'venue' else []}
    review = {'records': {}}
    if actionable and rows:
        review['records']['open-key'] = {'active': True, 'manual_action_required': True,
            'next_action': 'Confirm the serving entrance', 'reasons': ['Pin remains approximate'],
            'pub_name': rows[0]['pub_name'], 'place_name': rows[0]['place_name'], 'postcode': rows[0]['postcode'],
            'status': 'unverified'}
    return {
        'pubs.csv': row_bytes.getvalue().encode(),
        'latest-bass-directory.pdf': pdf,
        'directory-meta.json': json.dumps({'directory_label': edition}).encode(),
        'directory-release.json': json.dumps({'locations': locations}).encode(),
        'coordinate-verification.json': json.dumps({'records': records}).encode(),
        'location-review.json': json.dumps(review).encode(),
    }


class PublicationReportTests(unittest.TestCase):
    row = {'venue_id': 'venue-1', 'pub_name': 'Example Inn', 'place_name': 'Bristol',
           'postcode': 'BS1 1AA', 'pg': 'Perm', 'dispense': 'H', 'country': 'England',
           'area': 'Somerset', 'last': '2026 Q3', 'notes': ''}

    def report(self, previous, current):
        snapshots = {'old': snapshot_files(**previous), 'new': snapshot_files(**current)}

        def show(_commit, path):
            return snapshots[_commit][path]

        with patch('build_change_report._git_show', side_effect=show), patch(
                'build_change_report.subprocess.run', side_effect=[
                    SimpleNamespace(stdout='a' * 40), SimpleNamespace(stdout='b' * 40)]):
            return published_report('new', 'old', '2026-11-27T08:00:00Z')

    def test_pdf_only_revision_is_a_reportable_publication(self):
        report = self.report({'rows': [dict(self.row)], 'pdf': b'%PDF-old'},
                             {'rows': [dict(self.row)], 'pdf': b'%PDF-revised'})
        self.assertTrue(report['publication']['pdf_changed'])
        self.assertTrue(report['summary']['has_changes'])
        self.assertEqual(report['edition'], 'November 2026 Bass directory')
        self.assertEqual(report['published_on'], '2026-11-27T08:00:00Z')

    def test_new_listing_contains_pin_evidence_and_both_map_routes(self):
        new = dict(self.row, venue_id='venue-2', pub_name='New Inn')
        report = self.report({'rows': [dict(self.row)]}, {'rows': [dict(self.row), new]})
        added = next(item for item in report['added_publications'] if item['listing']['pub_name'] == 'New Inn')
        self.assertEqual(added['location']['precision'], 'venue')
        self.assertEqual(added['evidence'][0]['url'], 'https://camra.org.uk/pubs/123')
        self.assertEqual(len(added['evidence']), 1)
        self.assertTrue(added['maps_url'].endswith('cid=123'))
        self.assertIn('verify the mapped entrance', next(iter(report['actionable_owner_items'].values()))['question'])

    def test_changes_are_reported_by_stable_id_including_guest_and_street(self):
        after = dict(self.row, pg='Guest', dispense='G')
        report = self.report({'rows': [dict(self.row)], 'street': 'Old Street'},
                             {'rows': [after], 'street': 'New Street'})
        fields = report['field_changes_by_venue_id'][0]['changes']
        self.assertEqual(fields['Bass classification'], {'previous': 'Permanent', 'candidate': 'Guest'})
        self.assertEqual(fields['Street address'], {'previous': 'Old Street', 'candidate': 'New Street'})
        self.assertNotIn('Last checked', fields)

    def test_postcode_only_addition_uses_search_fallback_without_claiming_business_identity(self):
        new = dict(self.row, venue_id='venue-2', pub_name='Unlocated Inn')
        report = self.report({'rows': [dict(self.row)]}, {'rows': [dict(self.row), new],
                             'precision': 'postcode'})
        added = next(item for item in report['added_publications'] if item['listing']['pub_name'] == 'Unlocated Inn')
        self.assertEqual(added['maps_label'], 'Search Google Maps')
        self.assertEqual(added['evidence'], [])
        self.assertIn('Unlocated+Inn', added['maps_url'])

    def test_email_includes_every_action_item_supplied_and_live_link(self):
        report = self.report({'rows': [dict(self.row)]}, {'rows': [dict(self.row)]})
        items = {'old-alert-key': {'question': 'Resolve address review', 'context': 'Reference missing',
                                   'pub_name': 'Example Inn', 'place_name': 'Bristol', 'postcode': 'BS1 1AA'}}
        body = format_publication(report, items)
        self.assertIn('Outstanding owner decisions:', body)
        self.assertIn('Resolve address review', body)
        self.assertIn('https://bassbeermap.com/', body)

    def test_email_hides_diagnostics_and_duplicate_urls(self):
        new = dict(self.row, venue_id='venue-2', pub_name='New Inn')
        report = self.report({'rows': [dict(self.row)]}, {'rows': [dict(self.row), new]})
        report['added_publications'][0]['location']['diagnostic_key'] = {'raw': None}
        report['commit'] = 'a' * 40
        body = format_publication(report)
        self.assertEqual(body.count('https://www.google.com/maps?cid=123'), 1)
        self.assertNotIn('diagnostic_key', body)
        self.assertNotIn('venue-2', body)
        self.assertNotIn('a' * 40, body)
        self.assertNotIn('pdf_sha256', body)
        self.assertNotIn('None', body)
        self.assertNotIn('PDF changed:', body)

    def test_bass_confirmation_date_is_reported_but_evidence_timestamps_are_not(self):
        after = dict(self.row, last='2026 Q4')
        report = self.report({'rows': [dict(self.row)], 'street': 'High Street',
                              'evidence_checked_at': '2026-11-10'},
                             {'rows': [after], 'street': 'High Street',
                              'evidence_checked_at': '2026-11-26'})
        changes = report['field_changes_by_venue_id'][0]['changes']
        self.assertEqual(changes['Last Bass confirmation'], {'previous': '2026 Q3', 'candidate': '2026 Q4'})
        self.assertNotIn('checked_at', str(changes))

    def test_effective_postcode_visitor_hint_and_coordinate_distance_are_human_changes(self):
        report = self.report({'rows': [dict(self.row)], 'effective_postcode': 'BS1 1AA'},
                             {'rows': [dict(self.row)], 'effective_postcode': 'BS1 1AB',
                              'visitor_hint': 'Use the canal bridge.', 'lat': 51.401, 'lng': -2.6})
        changes = report['field_changes_by_venue_id'][0]['changes']
        self.assertEqual(changes['Effective directions postcode'],
                         {'previous': 'BS1 1AA', 'candidate': 'BS1 1AB'})
        self.assertIn('canal bridge', changes['Visitor directions']['candidate'])
        self.assertEqual(changes['Coordinates']['previous'], {'lat': 51.4, 'lng': -2.6})
        self.assertGreater(changes['Coordinates']['distance_metres'], 100)

    def test_pdf_only_revision_is_simple_and_says_there_are_no_owner_decisions(self):
        report = self.report({'rows': [dict(self.row)], 'pdf': b'%PDF-old', 'actionable': False},
                             {'rows': [dict(self.row)], 'pdf': b'%PDF-revised', 'actionable': False})
        body = format_publication(report)
        self.assertIn('The source PDF was revised.', body)
        self.assertIn('The published listings and locations stayed the same.', body)
        self.assertIn('There are no outstanding owner decisions.', body)
        self.assertIn('The current edition contains 1 listing.', body)
        self.assertNotIn('Edition totals', body)
        self.assertNotIn('Validated commit', body)


if __name__ == '__main__':
    unittest.main()
