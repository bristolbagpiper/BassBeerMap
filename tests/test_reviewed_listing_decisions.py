import copy
import unittest
from datetime import date

from directory_release import FIELDS, permanent_id, reconcile, venue_key


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
    def test_correction_applies_on_every_raw_reimport_and_retains_identity(self):
        original = listing()
        other = listing('Other Inn', 'BS1 1AA')
        registry = registry_for(original, other)
        identifier = permanent_id(venue_key(original))
        other_id = permanent_id(venue_key(other))
        correction = review(expected={'pub_name': original['pub_name'], 'place_name': original['place_name'],
                                      'postcode': original['postcode']},
                            changes={'postcode': 'BS1 1AB', 'notes': 'corrected note'})
        decisions = {'listing_corrections': {venue_key(original): correction}}

        first_rows, first_registry, first_report = reconcile([original, other], registry, copy.deepcopy(decisions))
        self.assertEqual(first_report['errors'], [])
        corrected = next(row for row in first_rows if row['pub_name'] == 'Example Inn')
        self.assertEqual(corrected['postcode'], 'BS1 1AB')
        self.assertEqual(corrected['notes'], 'corrected note')
        self.assertEqual(corrected['venue_id'], identifier)
        self.assertEqual(next(row for row in first_rows if row['pub_name'] == 'Other Inn')['venue_id'], other_id)
        self.assertEqual(len(first_registry['venues'][identifier]['listing_reviews']), 1)

        second_rows, second_registry, second_report = reconcile([original, other], first_registry, copy.deepcopy(decisions))
        self.assertEqual(second_report['errors'], [])
        self.assertEqual(next(row for row in second_rows if row['pub_name'] == 'Example Inn')['venue_id'], identifier)
        self.assertEqual(len(second_registry['venues'][identifier]['listing_reviews']), 1)
        self.assertEqual(len(second_registry['venues'][identifier]['identity_reviews']), 1)
        self.assertEqual(second_registry['venues'][other_id]['listing']['postcode'], 'BS1 1AA')

    def test_malformed_or_mismatched_correction_fails_closed(self):
        original = listing()
        registry = registry_for(original)
        bad_decisions = [
            {'listing_corrections': {venue_key(original): review(expected={'pub_name': 'wrong',
                'place_name': original['place_name'], 'postcode': original['postcode']}, changes={'postcode': 'BS1 1AB'})}},
            {'listing_corrections': {venue_key(original): review(expected={'pub_name': original['pub_name'],
                'place_name': original['place_name'], 'postcode': original['postcode']}, changes={'pg': 'Guest'})}},
            {'listing_corrections': {venue_key(original): {'expected': {}, 'changes': {'postcode': 'BS1 1AB'}}}},
        ]
        for decisions in bad_decisions:
            with self.subTest(decisions=decisions):
                rows, _, report = reconcile([original], registry, decisions)
                self.assertTrue(report['errors'])
                self.assertEqual(rows, [])

    def test_correction_cannot_take_another_venue_alias(self):
        original = listing('Original Inn', 'BS1 1AA')
        owner = listing('Original Inn', 'BS1 1AB')
        registry = registry_for(original, owner)
        correction = review(expected={'pub_name': original['pub_name'], 'place_name': original['place_name'],
                                      'postcode': original['postcode']},
                            changes={'postcode': owner['postcode']})
        rows, _, report = reconcile([original], registry,
                                    {'listing_corrections': {venue_key(original): correction}})
        self.assertTrue(any('already owned by another permanent identity' in error for error in report['errors']))
        self.assertEqual(rows, [])

    def test_notes_correction_accepts_raw_and_canonical_reimports_idempotently(self):
        original = listing(notes='old note')
        registry = registry_for(original)
        identifier = permanent_id(venue_key(original))
        correction = review(expected={field: original[field] for field in FIELDS},
                            changes={'notes': 'reviewed note'})
        decisions = {'listing_corrections': {venue_key(original): correction}}

        canonical_rows, registry, report = reconcile([original], registry, decisions)
        self.assertEqual(report['errors'], [])
        self.assertEqual(canonical_rows[0]['notes'], 'reviewed note')
        canonical_rows, registry, report = reconcile(canonical_rows, registry, decisions)
        self.assertEqual(report['errors'], [])
        self.assertEqual(canonical_rows[0]['notes'], 'reviewed note')
        canonical_rows, registry, report = reconcile([original], registry, decisions)
        self.assertEqual(report['errors'], [])
        self.assertEqual(canonical_rows[0]['notes'], 'reviewed note')
        self.assertEqual(len(registry['venues'][identifier]['listing_reviews']), 1)

        changed = dict(original, pg='Guest')
        rows, _, blocked = reconcile([changed], registry, decisions)
        self.assertTrue(blocked['errors'])
        self.assertEqual(rows, [])

    def test_correction_rejects_conflicting_explicit_identity_decision(self):
        original, other = listing('Original Inn'), listing('Other Inn', 'BS1 1AC')
        registry = registry_for(original, other)
        target = dict(original, postcode='BS1 1AB')
        correction = review(expected={field: original[field] for field in ('pub_name', 'place_name', 'postcode')},
                            changes={'postcode': target['postcode']})
        decisions = {'listing_corrections': {venue_key(original): correction},
                     'identity_aliases': {venue_key(target): review(previous_key=venue_key(other), reference_postcode='BS1 1AB')}}
        rows, _, report = reconcile([original], registry, decisions)
        self.assertTrue(any('conflicts with another identity alias decision' in error for error in report['errors']))
        self.assertEqual(rows, [])

    def test_exclusion_skips_exact_known_listing_and_retains_pin_provenance(self):
        original = listing()
        other = listing('Other Inn')
        registry = registry_for(original, other)
        identifier = permanent_id(venue_key(original))
        excluded = review(expected={field: original[field] for field in FIELDS})
        rows, updated, report = reconcile([original, other], registry,
                                          {'listing_exclusions': {venue_key(original): excluded}})
        self.assertEqual(report['errors'], [])
        self.assertEqual([row['pub_name'] for row in rows], ['Other Inn'])
        entry = updated['venues'][identifier]
        self.assertEqual(entry['verified_pin']['lat'], 51.45)
        self.assertEqual(entry['listing_exclusions'], [excluded])
        self.assertEqual(entry['listing_reviews'][0]['kind'], 'exclusion')

    def test_changed_excluded_listing_is_blocked_without_silent_readdition(self):
        original = listing()
        registry = registry_for(original)
        excluded = review(expected={field: original[field] for field in FIELDS})
        decisions = {'listing_exclusions': {venue_key(original): excluded}}
        accepted, registry, report = reconcile([original], registry, decisions)
        self.assertEqual(accepted, [])
        self.assertEqual(report['errors'], [])
        changed = dict(original, last='2026 Q4')
        rows, unchanged, blocked = reconcile([changed], registry, {})
        self.assertTrue(any('previously excluded listing' in error or 'excluded listing changed' in error
                            for error in blocked['errors']))
        self.assertEqual(rows, [])
        self.assertEqual(unchanged['venues'][permanent_id(venue_key(original))]['listing_exclusions'], [excluded])

    def test_exclusion_requires_known_identity_and_valid_review_metadata(self):
        original = listing()
        unknown = review(expected={field: original[field] for field in FIELDS})
        rows, _, report = reconcile([original], {'venues': {}},
                                    {'listing_exclusions': {venue_key(original): unknown}})
        self.assertTrue(report['errors'])
        self.assertEqual(rows, [])
        registry = registry_for(original)
        malformed = {'expected': {field: original[field] for field in FIELDS}, 'reason': 'exclude'}
        rows, _, report = reconcile([original], registry,
                                    {'listing_exclusions': {venue_key(original): malformed}})
        self.assertTrue(report['errors'])
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main()
