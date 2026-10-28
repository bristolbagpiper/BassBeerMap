import unittest

from approve_location import (
    approved_commands,
    extract_camra_heading,
    manual_name_matches,
    validate_google_place,
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
