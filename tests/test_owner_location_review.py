import copy
import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from coordinate_sources import recheck_pin
from owner_location_review import (
    confirmation_fresh,
    current_confirmation,
    google_finding_assessment,
    classify_review,
    source_name_acknowledged,
    pending_building_review,
)
from tests.test_directory_release import pin as base_pin, row as base_row


SOURCE_URL = "https://camra.org.uk/pubs/black-cat-rose-lane"


def row():
    result = base_row("Black Cat", "L18 8AG")
    result.update(venue_id="venue-black-cat", place_name="Liverpool: Mossley Hill", area="Merseyside", country="England")
    return result


def pin():
    result = base_pin()
    result.update(lat=53.3784743, lng=-2.9166268)
    result["evidence"][0].update(
        url=SOURCE_URL,
        reference_name="Black Cat Rose Lane, Liverpool",
        reference_address="102 Rose Lane",
        reference_postcode="L18 8AG",
        reference_coordinate={"lat": result["lat"], "lng": result["lng"]},
    )
    result["owner_confirmation"] = {
        "schema_version": 1,
        "scope": "displayed_venue_building",
        "listing": {key: result_value for key, result_value in {
            "venue_id": "venue-black-cat", "pub_name": "Black Cat",
            "place_name": "Liverpool: Mossley Hill", "postcode": "L18 8AG",
            "area": "Merseyside", "country": "England",
        }.items()},
        "approved_coordinate": {"lat": result["lat"], "lng": result["lng"]},
        "checked_at": (date.today() - timedelta(days=10)).isoformat(),
        "reviewed_by": "venue owner",
        "owner_instruction": "This is the Rose Lane venue building.",
        "source_snapshots": [{
            "url": SOURCE_URL,
            "name": "Black Cat Rose Lane, Liverpool",
            "postcode": "L18 8AG",
            "address": "102 Rose Lane",
        }],
    }
    return result


class ConfirmationBindingTests(unittest.TestCase):
    def test_building_request_requires_its_own_resolution_and_does_not_follow_another_pin(self):
        accepted = pin()
        accepted['manual_review_request'] = dict(schema_version=1, scope='building_coordinate',
            id='review-one',venue_id=row()['venue_id'],questioned_coordinate={k:accepted[k] for k in ('lat','lng')},
            reason='Named marker differs; no incorrect pin established',source_url=SOURCE_URL)
        self.assertIsNotNone(pending_building_review(row(), accepted))
        accepted['owner_confirmation']['resolved_review_request_ids']=['review-one']
        self.assertIsNone(pending_building_review(row(), accepted))
        accepted['owner_confirmation']['resolved_review_request_ids']=['older-review']
        self.assertIsNotNone(pending_building_review(row(), accepted))
        accepted['lat']+=0.0001
        self.assertIsNone(pending_building_review(row(), accepted))

    def test_audit_confidence_does_not_carry_a_confirmation_to_another_point(self):
        from full_location_audit import assess
        accepted = pin()
        entry = {'verified_pin': accepted, 'location_approvals': [{'historical': True}]}
        self.assertTrue(assess(row(), entry, lambda *args, **kwargs: [])['owner_reviewed'])
        accepted['lat'] += 0.00001
        self.assertFalse(assess(row(), entry, lambda *args, **kwargs: [])['owner_reviewed'])

    def test_confirmation_is_bound_to_exact_listing_and_coordinate(self):
        listing, accepted = row(), pin()
        self.assertIsNotNone(current_confirmation(listing, accepted))
        mutations = [
            ("venue_id", "other-venue"),
            ("pub_name", "Black Cat Bar"),
            ("place_name", "Liverpool: Central"),
            ("postcode", "L18 8AJ"),
            ("area", "Lancashire"),
            ("country", "Wales"),
        ]
        for field, changed in mutations:
            changed_row = dict(listing, **{field: changed})
            with self.subTest(field=field):
                self.assertIsNone(current_confirmation(changed_row, accepted))
        for field, changed in (("lat", 53.4018), ("lng", -2.9801)):
            changed_pin = copy.deepcopy(accepted)
            changed_pin[field] = changed
            with self.subTest(pin_field=field):
                self.assertIsNone(current_confirmation(listing, changed_pin))

    def test_malformed_future_and_blank_authority_fail_closed(self):
        listing, accepted = row(), pin()
        self.assertIsNone(current_confirmation(listing, accepted, date.today() - timedelta(days=20)))
        for field, value in (("reviewed_by", "  "), ("owner_instruction", ""),
                             ("scope", "postal_delivery"), ("schema_version", True)):
            altered = copy.deepcopy(accepted)
            altered["owner_confirmation"][field] = value
            with self.subTest(field=field):
                self.assertIsNone(current_confirmation(listing, altered))
        malformed_date = copy.deepcopy(accepted)
        malformed_date["owner_confirmation"]["checked_at"] = "not-a-date"
        self.assertIsNone(current_confirmation(listing, malformed_date))

    def test_freshness_boundary_and_expiry(self):
        listing, accepted = row(), pin()
        review = accepted["owner_confirmation"]
        review["checked_at"] = (date.today() - timedelta(days=179)).isoformat()
        self.assertTrue(confirmation_fresh(listing, accepted))
        review["checked_at"] = (date.today() - timedelta(days=180)).isoformat()
        self.assertFalse(confirmation_fresh(listing, accepted))


class AcknowledgedSourceTests(unittest.TestCase):
    def test_acknowledges_only_same_source_primary_name_address_and_postcode(self):
        listing, accepted = row(), pin()
        same_primary_seo_title = {
            "name": "Black Cat Rose Lane, Liverpool - Pub opening times",
            "address": "102 Rose Lane", "postcode": "L18 8AG",
        }
        self.assertTrue(source_name_acknowledged(listing, accepted, SOURCE_URL, same_primary_seo_title))
        variants = [
            ("name", "Black Cat Rose Lane Annex, Liverpool"),
            ("address", "27 Rose Lane"),
            ("postcode", "L18 8AJ"),
        ]
        for key, value in variants:
            changed = dict(same_primary_seo_title, **{key: value})
            with self.subTest(field=key):
                self.assertFalse(source_name_acknowledged(listing, accepted, SOURCE_URL, changed))
        self.assertFalse(source_name_acknowledged(listing, accepted, SOURCE_URL + "-other", same_primary_seo_title))

    def test_malformed_source_snapshots_are_not_acknowledged(self):
        listing, accepted = row(), pin()
        for snapshots in (None, {}, [None], [{"url": SOURCE_URL, "name": "Black Cat Rose Lane"}]):
            altered = copy.deepcopy(accepted)
            altered["owner_confirmation"]["source_snapshots"] = snapshots
            with self.subTest(snapshots=snapshots):
                self.assertFalse(source_name_acknowledged(
                    listing, altered, SOURCE_URL,
                    {"name": "Black Cat Rose Lane", "address": "102 Rose Lane", "postcode": "L18 8AG"},
                ))


class ReviewClassificationTests(unittest.TestCase):
    def test_google_combined_marker_and_trading_flags_keep_both_actions(self):
        result = google_finding_assessment({'reason': 'Google named-place marker differs by more than 50 metres; Google indicates a possible closure; trading status needs review'})
        self.assertEqual(result['review_category'], 'location_and_trading_check')
        self.assertTrue(result['manual_action_required'])
        self.assertIn('building', result['next_action'])
        self.assertIn('trading', result['next_action'])
        self.assertNotIn('not the pin position', result['next_action'])

    def test_missing_or_source_warning_does_not_call_pin_incorrect(self):
        for record, expected in (
            ({"active": True, "reasons": ["Manual location evidence needs review"]}, "needs_verification"),
            ({"active": True, "reasons": ["CAMRA source unavailable"]}, "source_warning"),
        ):
            with self.subTest(record=record):
                outcome = classify_review(record)
                self.assertEqual(outcome["review_category"], expected)
                self.assertTrue(outcome["next_action"])
                self.assertNotIn("incorrect pin", outcome["next_action"].casefold().replace("no incorrect pin has been established", ""))
        self.assertEqual(google_finding_assessment({"reason": "Coordinates are missing"})["review_category"], "source_warning")

    def test_identity_trading_and_unverified_points_request_specific_action(self):
        cases = (
            ({"active": True, "reasons": ["Venue name changed"]}, "identity_check", "business changed"),
            ({"active": True, "reasons": ["Trading status needs review"]}, "trading_check", "trading"),
            ({"active": True, "reasons": ["Approximate point needs manual location evidence"]}, "needs_verification", "evidence"),
        )
        for record, category, action_fragment in cases:
            with self.subTest(category=category):
                outcome = classify_review(record)
                self.assertEqual(outcome["review_category"], category)
                self.assertTrue(outcome["manual_action_required"])
                self.assertIn(action_fragment, outcome["next_action"].casefold())


class RecheckIntegrationTests(unittest.TestCase):
    @staticmethod
    def camra_page(name="Black Cat Rose Lane, Liverpool - Pub opening times", postcode="L18 8AG",
                   lat=53.3784743, lng=-2.9166268, address="102 Rose Lane"):
        payload = {"@type": "BarOrPub", "name": name,
                   "address": {"postalCode": postcode, "streetAddress": address},
                   "geo": {"latitude": lat, "longitude": lng}}
        return '<script type="application/ld+json">' + json.dumps(payload) + '</script>'

    def test_owner_acknowledged_seo_suffix_confirms_same_primary_name(self):
        with patch("coordinate_sources.fetch_text", return_value=self.camra_page()):
            result = recheck_pin(row(), pin())
        self.assertEqual(result["status"], "confirmed")

    def test_brand_change_still_conflicts(self):
        with patch("coordinate_sources.fetch_text", return_value=self.camra_page(name="Kova, Liverpool")):
            result = recheck_pin(row(), pin())
        self.assertEqual(result["status"], "conflict")
        self.assertIn("name changed", result["reason"])

    def test_black_cat_marketing_change_does_not_require_a_new_owner_confirmation(self):
        accepted = pin()
        accepted.pop('owner_confirmation')
        accepted['evidence'][0]['reference_name'] = 'Black Cat Rose Lane, Liverpool - 3 Rotating Ales'
        with patch('coordinate_sources.fetch_text', return_value=self.camra_page(
                name='Black Cat Rose Lane, Liverpool - Rotating Cask Beers')):
            self.assertEqual(recheck_pin(row(), accepted)['status'], 'confirmed')

    def test_parr_street_branch_cannot_restore_the_rose_lane_pin(self):
        with patch('coordinate_sources.fetch_text', return_value=self.camra_page(
                name='Black Cat Parr Street, Liverpool', postcode='L1 4JN',
                address='25 Parr Street', lat=53.4016502, lng=-2.9796419)):
            self.assertEqual(recheck_pin(row(), pin())['status'], 'conflict')

    def test_approved_comma_clause_cannot_be_replaced_by_another_branch(self):
        accepted = pin()
        accepted.pop('owner_confirmation')
        listing = dict(row(), pub_name='Pickled Pig at the Bankhouse')
        accepted['evidence'][0]['reference_name'] = 'Pickled Pig, at the Bankhouse, Stafford - Music'
        with patch('coordinate_sources.fetch_text', return_value=self.camra_page(
                name='Pickled Pig, at Another House, Stafford - Music')):
            self.assertEqual(recheck_pin(listing, accepted)['status'], 'conflict')

    def test_acknowledged_name_cannot_override_postcode_or_coordinate_conflict(self):
        for kwargs, reason in (
            ({"postcode": "L18 8AJ"}, "postcode"),
            ({"lat": 53.3792}, "coordinate"),
        ):
            with self.subTest(reason=reason), patch(
                "coordinate_sources.fetch_text", return_value=self.camra_page(**kwargs)
            ):
                result = recheck_pin(row(), pin())
                self.assertEqual(result["status"], "conflict")
                self.assertIn(reason, result["reason"])


if __name__ == "__main__":
    unittest.main()
