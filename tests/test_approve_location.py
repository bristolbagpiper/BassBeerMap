import unittest

from approve_location import (
    approved_commands,
    extract_camra_heading,
    manual_name_matches,
    validate_google_place,
    validate_retained_point,
)

from coordinate_sources import name_matches


VENUE_ID = "v-152d1ab1-df05-55e1-a620-e4f9d08cdfd6"
PLACE_URL = (
    "https://www.google.com/maps/place/Black+Cat+Parr+Street/"
    "@53.4016502,-2.9822168,512m/data=!3m2!1e3!4b1!4m14!1m7!3m6!"
    "1s0x487b21e290e5b141:0xf01add51fe76ea7!2sBlack+Cat+Parr+Street!"
    "8m2!3d53.4016502!4d-2.9796419!16s%2Fg%2F11zkc1n1lr!3m5!"
    "1s0x487b21e290e5b141:0xf01add51fe76ea7!8m2!3d53.4016502!"
    "4d-2.9796419!16s%2Fg%2F11zkc1n1lr"
)
ROW = {"pub_name": "Black Cat Bar", "place_name": "Liverpool: Central", "postcode": "L1 4JN"}
CENTRE = {"lat": 53.4022, "lng": -2.98134}


def event(body, login="bristolbagpiper", user_type="User", issue=5):
    return {
        "comment": {
            "user": {"login": login, "type": user_type},
            "body": body,
        },
        "issue": {"number": issue},
    }


class ApprovalCommandTests(unittest.TestCase):
    def test_retained_point_accepts_camra_heading_alias_and_exact_former_name(self):
        url = 'https://camra.org.uk/pubs/131286'
        command = {'lat': 52.2076353, 'lng': 0.1267339, 'url': url}
        row = {'pub_name': 'The Rad (was St Radegund)', 'postcode': 'CB1 1LD'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': 52.207599, 'lng': 0.126732, 'postcode': 'CB1 1LD',
                  'address': '129 King Street', 'name': 'St. Radegund, Cambridge - Cambridge\'s Smallest Pub'}
        page = '<h1>St. Radegund, Cambridge (Rad)</h1>'
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, source, '<h1>St. Radegund, Cambridge (Other)</h1>')
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, name='Different Arms'), page)

    def test_retained_point_requires_same_saved_point_and_corroborating_source(self):
        url = 'https://camra.org.uk/pubs/145783'
        command = {'lat': 52.8079276, 'lng': -2.1168364, 'url': url}
        row = {'pub_name': 'Pickled Pig at the Bankhouse', 'postcode': 'ST16 2JU'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'],
                                  'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': 52.807928, 'lng': -2.11676, 'postcode': 'ST16 2JU',
                  'address': '5 Salter Street', 'name': 'Pickled Pig, at the Bankhouse, Stafford'}
        self.assertEqual(validate_retained_point(command, row, entry, source), entry['verified_pin'])
        with self.assertRaises(ValueError):
            validate_retained_point(dict(command, lat=52.808), row, entry, source)
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, name='Other Pub'))
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, postcode='ST16 1AA'))
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, lat=53.1))

    def test_retained_point_accepts_exact_documented_previous_name_only(self):
        url = 'https://camra.org.uk/pubs/113335'
        command = {'lat': 53.053271, 'lng': -1.405467, 'url': url}
        row = {'pub_name': 'Talbot Taphouse', 'postcode': 'DE5 3LT'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': command['lat'], 'lng': command['lng'], 'postcode': 'DE5 3LT',
                  'address': '1 Butterley Hill', 'name': 'Talbot, Ripley'}
        page = '<dt class="text-sm">Previous Names</dt><dd>Talbot Taphouse</dd>'
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        for other in ('<p>Talbot Taphouse</p>', '<dt>Previous Names</dt><dd>Talbot Taphouse Other</dd>'):
            with self.assertRaises(ValueError):
                validate_retained_point(command, row, entry, source, other)

    def test_pelican_reviewed_bilingual_name_is_specific(self):
        self.assertTrue(manual_name_matches("Pelican Inn/Tafarn Y Pelican", "The Pelican Inn"))
        self.assertFalse(manual_name_matches("Pelican Inn/Tafarn Y Pelican", "The Pelican Arms"))
        self.assertFalse(manual_name_matches("Other Inn/Tafarn Y Pelican", "The Pelican Inn"))

    def test_parenthetical_social_club_label_allows_primary_venue_name(self):
        self.assertTrue(
            manual_name_matches("Brookfield (Barrow Social Club)", "The Brookfield")
        )

    def test_parenthetical_label_does_not_hide_mismatched_primary_name(self):
        self.assertFalse(
            manual_name_matches("New Brookfield (Barrow Social Club)", "The Brookfield")
        )

    def test_camra_town_prefixed_title_matches_listing_locality(self):
        self.assertTrue(
            name_matches(
                "Constitutional Club (PMC)",
                "Swadlincote Constitutional Club, Swadlincote",
                "Swadlincote: Central",
            )
        )
        self.assertFalse(
            name_matches(
                "Constitutional Club (PMC)",
                "Swadlincote Conservative Club, Swadlincote",
                "Swadlincote: Central",
            )
        )

    def test_google_place_command_is_parsed_with_explicit_point_and_address(self):
        command = (
            f'/verify-location {VENUE_ID} google 53.4016502 -2.9796419 '
            f'{PLACE_URL} "Black Cat Parr Street" "25 Parr St, Liverpool L1 4JN"'
        )
        result = approved_commands(event(command), "bristolbagpiper")
        self.assertEqual(result, [{
            "kind": "google",
            "venue_id": VENUE_ID,
            "lat": 53.4016502,
            "lng": -2.9796419,
            "url": PLACE_URL,
            "reference_name": "Black Cat Parr Street",
            "reference_address": "25 Parr St, Liverpool L1 4JN",
        }])

    def test_manual_name_accepts_only_an_omitted_final_arms_suffix(self):
        self.assertTrue(
            manual_name_matches("Earl of Chesterfield Arms", "The Earl of Chesterfield")
        )
        self.assertFalse(
            manual_name_matches("Earl of Chesterfield Arms", "The Earl of Chesterfield Golf Course")
        )

    def test_google_place_accepts_the_exact_earl_of_chesterfield_arms_alias(self):
        url = (
            "https://www.google.com/maps/place/The+Earl+of+Chesterfield/"
            "@52.9754979,-1.012228,40m/data=!3m1!1e3!4m6!3m5!"
            "1s0x4879c617eaf89019:0x5398220fe768072f!8m2!"
            "3d52.9755234!4d-1.0120792!16s%2Fg%2F1v2yyd3r"
        )
        validate_google_place(
            {
                "lat": 52.9755234,
                "lng": -1.0120792,
                "url": url,
                "reference_name": "The Earl of Chesterfield",
                "reference_address": "Main Rd, Shelford, Nottingham NG12 1EQ",
            },
            {"pub_name": "Earl of Chesterfield Arms", "postcode": "NG12 1EQ"},
            {"lat": 52.975953, "lng": -1.012223},
        )

    def test_existing_camra_command_remains_supported(self):
        url = "https://camra.org.uk/pubs/173888"
        result = approved_commands(
            event(f"/verify-location {VENUE_ID} {url}"), "bristolbagpiper"
        )
        self.assertEqual(result, [{"kind": "camra", "venue_id": VENUE_ID, "url": url}])

    def test_only_repository_owner_can_submit(self):
        with self.assertRaisesRegex(ValueError, "Only the repository owner"):
            approved_commands(
                event(f"/verify-location {VENUE_ID} https://camra.org.uk/pubs/173888", login="someone-else"),
                "bristolbagpiper",
            )

    def test_google_place_requires_matching_marker_coordinates_and_full_postcode(self):
        command = {
            "lat": 53.4016502,
            "lng": -2.9796419,
            "url": PLACE_URL,
            "reference_name": "Black Cat Parr Street",
            "reference_address": "25 Parr St, Liverpool L1 4JN",
        }
        validate_google_place(command, ROW, CENTRE)

    def test_google_place_rejects_coordinates_different_from_named_marker(self):
        command = {
            "lat": 53.4022,
            "lng": -2.98134,
            "url": PLACE_URL,
            "reference_name": "Black Cat Parr Street",
            "reference_address": "25 Parr St, Liverpool L1 4JN",
        }
        with self.assertRaisesRegex(ValueError, "must match the named Google Maps place marker"):
            validate_google_place(command, ROW, CENTRE)

    def test_camra_documented_alias_can_corroborate_google_place_name(self):
        url = (
            "https://www.google.com/maps/place/Milford+Social+Club/"
            "@53.0027926,-1.4805613,113m/data=!3m1!1e3!4m6!3m5!"
            "1s0x4879f30d3be736e7:0xa35191fd161dbc93!8m2!"
            "3d53.002819!4d-1.4804092!16s%2Fg%2F11clsg50h9"
        )
        command = {
            "lat": 53.002819,
            "lng": -1.4804092,
            "url": url,
            "reference_name": "Milford Social Club",
            "reference_address": "5 Chevin Rd, Milford, Belper DE56 0QH",
        }
        row = {
            "pub_name": "Milford Sunshine Social Club (PMC)",
            "place_name": "Milford",
            "postcode": "DE56 0QH",
        }
        source = {
            "lat": 53.002795,
            "lng": -1.480413,
            "name": "Milford Social Club, Milford",
            "postcode": "DE56 0QH",
            "address": "5 Chevin View",
            "locality": "Milford",
        }
        heading = "Milford Social Club, Milford (Sunshine Club)"
        self.assertEqual(extract_camra_heading(f"<h1>{heading}</h1>"), heading)
        self.assertTrue(
            validate_google_place(
                command, row, {"lat": 53.0028, "lng": -1.4805}, source, heading
            )
        )

    def test_camra_documented_upstairs_companion_can_corroborate_composite_listing(self):
        url = (
            "https://www.google.com/maps/place/The+Crew/"
            "@52.5224146,-1.4736173,81m/data=!3m1!1e3!4m6!3m5!"
            "1s0x48774e02751e41b5:0x82b0e77e8f8e6295!8m2!"
            "3d52.5224838!4d-1.4733998!16s%2Fg%2F1vd70v2h"
        )
        command = {
            "lat": 52.5224838,
            "lng": -1.4733998,
            "url": url,
            "reference_name": "The Crew",
            "reference_address": "75 Queens Rd, Nuneaton CV11 5LA",
        }
        row = {
            "pub_name": "Crew and Queens Hall",
            "postcode": "CV11 5LA",
        }
        source = {
            "lat": 52.52246,
            "lng": -1.47342,
            "name": "Crew, Nuneaton",
            "postcode": "CV11 5LA",
        }
        page = (
            "<h1>Crew, Nuneaton - Rock Memorabilia &amp; Live Music</h1>"
            "<p>Queens Hall rock venue is located upstairs with tribute bands.</p>"
        )
        self.assertTrue(
            validate_google_place(
                command, row, {"lat": 52.5224, "lng": -1.4735},
                source, "Crew, Nuneaton - Rock Memorabilia & Live Music", page,
            )
        )

    def test_camra_companion_name_requires_explicit_co_location_context(self):
        url = (
            "https://www.google.com/maps/place/The+Crew/"
            "@52.5224146,-1.4736173,81m/data=!3m1!1e3!4m6!3m5!"
            "1s0x48774e02751e41b5:0x82b0e77e8f8e6295!8m2!"
            "3d52.5224838!4d-1.4733998!16s%2Fg%2F1vd70v2h"
        )
        command = {
            "lat": 52.5224838,
            "lng": -1.4733998,
            "url": url,
            "reference_name": "The Crew",
            "reference_address": "75 Queens Rd, Nuneaton CV11 5LA",
        }
        row = {"pub_name": "Crew and Queens Hall", "postcode": "CV11 5LA"}
        source = {
            "lat": 52.52246,
            "lng": -1.47342,
            "name": "Crew, Nuneaton",
            "postcode": "CV11 5LA",
        }
        with self.assertRaisesRegex(ValueError, "alias or co-located venue"):
            validate_google_place(
                command, row, {"lat": 52.5224, "lng": -1.4735}, source,
                "Crew, Nuneaton", "<p>Queens Hall hosts several bands.</p>",
            )

    def test_google_name_mismatch_still_fails_without_camra(self):
        command = {
            "lat": 53.002819,
            "lng": -1.4804092,
            "url": "https://www.google.com/maps/place/Milford+Social+Club/"
                   "@53.0027926,-1.4805613,113m/data=!3m1!1e3!4m6!3m5!"
                   "1s0x4879f30d3be736e7:0xa35191fd161dbc93!8m2!"
                   "3d53.002819!4d-1.4804092!16s%2Fg%2F11clsg50h9",
            "reference_name": "Milford Social Club",
            "reference_address": "5 Chevin Rd, Milford, Belper DE56 0QH",
        }
        with self.assertRaisesRegex(ValueError, "does not identify the listed venue"):
            validate_google_place(
                command,
                {"pub_name": "Milford Sunshine Social Club (PMC)", "postcode": "DE56 0QH"},
                {"lat": 53.0028, "lng": -1.4805},
            )

    def test_camra_alias_requires_exact_postcode_and_nearby_point(self):
        command = {
            "lat": 53.002819,
            "lng": -1.4804092,
            "url": "https://www.google.com/maps/place/Milford+Social+Club/"
                   "@53.0027926,-1.4805613,113m/data=!3m1!1e3!4m6!3m5!"
                   "1s0x4879f30d3be736e7:0xa35191fd161dbc93!8m2!"
                   "3d53.002819!4d-1.4804092!16s%2Fg%2F11clsg50h9",
            "reference_name": "Milford Social Club",
            "reference_address": "5 Chevin Rd, Milford, Belper DE56 0QH",
        }
        row = {"pub_name": "Milford Sunshine Social Club (PMC)", "postcode": "DE56 0QH"}
        source = {
            "lat": 53.002795,
            "lng": -1.480413,
            "name": "Milford Social Club, Milford",
            "postcode": "DE56 0QH",
        }
        heading = "Milford Social Club, Milford (Sunshine Club)"
        with self.assertRaisesRegex(ValueError, "exact full postcode"):
            validate_google_place(
                command, row, {"lat": 53.0028, "lng": -1.4805},
                dict(source, postcode="DE56 0QB"), heading
            )
        with self.assertRaisesRegex(ValueError, "within 50 metres"):
            validate_google_place(
                command, row, {"lat": 53.0028, "lng": -1.4805},
                dict(source, lat=53.0035), heading
            )
        with self.assertRaisesRegex(ValueError, "document every missing listing-name word"):
            validate_google_place(
                command, row, {"lat": 53.0028, "lng": -1.4805},
                source, "Milford Social Club, Milford (Club)"
            )

    def test_google_command_parses_camra_alias_source(self):
        google_url = (
            "https://www.google.com/maps/place/Milford+Social+Club/"
            "@53.0027926,-1.4805613,113m/data=!3m1!1e3!4m6!3m5!"
            "1s0x4879f30d3be736e7:0xa35191fd161dbc93!8m2!"
            "3d53.002819!4d-1.4804092!16s%2Fg%2F11clsg50h9"
        )
        command = (
            f'/verify-location {VENUE_ID} google 53.002819 -1.4804092 '
            f'{google_url} "Milford Social Club" '
            '"5 Chevin Rd, Milford, Belper DE56 0QH" '
            'camra https://camra.org.uk/pubs/milford-social-club-milford-113188'
        )
        result = approved_commands(event(command), "bristolbagpiper")
        self.assertEqual(
            result[0]["corroborating_source_url"],
            "https://camra.org.uk/pubs/milford-social-club-milford-113188",
        )

    def test_google_place_requires_matching_full_postcode(self):
        command = {
            "lat": 53.4016502,
            "lng": -2.9796419,
            "url": PLACE_URL,
            "reference_name": "Black Cat Parr Street",
            "reference_address": "25 Parr Street, Liverpool L1 4HY",
        }
        with self.assertRaisesRegex(ValueError, "full postcode"):
            validate_google_place(command, ROW, CENTRE)


if __name__ == "__main__":
    unittest.main()


