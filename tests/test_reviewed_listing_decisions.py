import unittest
from datetime import date

from directory_release import build_ledger, permanent_id, reconcile, venue_key


def listing(name='Example Inn', postcode='BS1 1AA', notes=''):
    return dict(country='England', area='Somerset', pub_name=name, place_name='Bristol', postcode=postcode,
                pg='Perm', last='2026 Q3', dispense='H', notes=notes)


def registry_for(*rows):
    venues = {}
    for row in rows:
        key = venue_key(row)
        identifier = permanent_id(key)
        snapshot = dict(row, venue_id=identifier)
        venues[identifier] = {'aliases': [key], 'current_key': key,
                              'listing': snapshot, 'verified_pin': {'lat': 51.45, 'lng': -2.59,
                              'source': 'reviewed', 'evidence': []}}
    return {'schema_version': 1, 'venues': venues}


def review(**extra):
    return dict(reason='checked against the source directory', source_url='https://example.test/source',
                checked_at=date.today().isoformat(), **extra)


class ReviewedListingDecisionTests(unittest.TestCase):
    def test_historical_corrections_cannot_overwrite_source_and_remain_searchable(self):
        source = listing(notes='PDF notes')
        registry = registry_for(source)
        key = venue_key(source)
        identifier = permanent_id(key)
        correction = review(expected=source, changes={
            'pub_name': 'Alternate Inn', 'postcode': 'BS1 1AB', 'notes': 'External trading note'})
        registry['venues'][identifier]['listing_reviews'] = [
            dict(kind='correction', original_key=key, original=source, decision=correction)]
        decisions = {'listing_corrections': {key: correction}}

        rows, updated, report = reconcile([source], registry, decisions)

        self.assertEqual(report['errors'], [])
        self.assertEqual(rows, [dict(source, venue_id=identifier)])
        self.assertEqual(updated['venues'][identifier]['listing_reviews'],
                         registry['venues'][identifier]['listing_reviews'])
        record = build_ledger(rows, updated)['records'][key]
        self.assertEqual(record['alternate_name'], 'Alternate Inn')
        self.assertEqual(record['postcode'], source['postcode'])

    def test_exclusion_is_preserved_as_history_and_cannot_remove_current_pdf_listing(self):
        source = listing()
        key = venue_key(source)
        identifier = permanent_id(key)
        registry = registry_for(source)
        old_exclusion = review(expected=source)
        registry['venues'][identifier]['listing_exclusions'] = [old_exclusion]
        decisions = {'listing_exclusions': {key: old_exclusion}}
        updated_source = dict(source, pg='Guest', last='2026 Q4')

        rows, updated, report = reconcile([updated_source], registry, decisions)

        self.assertEqual(report['errors'], [])
        self.assertEqual(rows, [dict(updated_source, venue_id=identifier)])
        self.assertEqual(updated['venues'][identifier]['listing_exclusions'], [old_exclusion])
        self.assertEqual(updated['venues'][identifier]['verified_pin'],
                         registry['venues'][identifier]['verified_pin'])

    def test_reference_postcode_retains_the_pin_and_updates_neither_source_field_nor_history(self):
        previous = listing(postcode='BS1 1AB')
        source = listing(postcode='BS1 1AA')
        registry = registry_for(previous)
        identifier = permanent_id(venue_key(previous))
        decision = review(previous_key=venue_key(previous), reference_postcode=previous['postcode'],
                          use_reference_for_directions=True)
        decisions = {'identity_aliases': {venue_key(source): decision}}

        rows, updated, report = reconcile([source], registry, decisions)
        self.assertEqual(report['errors'], [])
        self.assertEqual(rows, [dict(source, venue_id=identifier)])
        entry = updated['venues'][identifier]
        self.assertEqual(entry['verified_pin']['lat'], registry['venues'][identifier]['verified_pin']['lat'])
        self.assertEqual(entry['verified_pin']['metadata_issue']['reference'], previous['postcode'])
        self.assertTrue(entry['verified_pin']['metadata_issue']['use_reference_for_directions'])
        again, repeated, report = reconcile([source], updated, decisions)
        self.assertEqual(report['errors'], [])
        self.assertEqual(again, rows)
        self.assertEqual(repeated, updated)
