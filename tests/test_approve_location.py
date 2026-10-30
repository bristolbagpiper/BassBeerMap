import unittest

from approve_location import (
    approved_commands,
    extract_camra_heading,
    manual_name_matches,
    validate_google_place,
    validate_retained_point,
    camra_heading_matches_listing,
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
    def test_retained_omitted_ye_requires_full_name_and_heading(self):
        url = 'https://camra.org.uk/pubs/186936'
        command = {'lat': 54.775832, 'lng': -1.5815745, 'url': url}
        row = {'pub_name': 'Ye Old Elm Tree', 'postcode': 'DH1 4PS'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': command['lat'], 'lng': command['lng'], 'postcode': 'DH1 4PS',
                  'address': '12 Crossgate', 'name': 'Old Elm Tree, Durham - Acoustic Nights & Patio'}
        page = '<h1>Old Elm Tree, Durham</h1>'
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        for bad_source, bad_page in ((dict(source, name='Old Oak Tree, Durham'), page),
                                     (source, '<h1>Old Oak Tree, Durham</h1>')):
            with self.assertRaises(ValueError):
                validate_retained_point(command, row, entry, bad_source, bad_page)

    def test_retained_current_name_requires_documented_former_name(self):
        url = 'https://camra.org.uk/pubs/131517'
        command = {'lat': 52.037091, 'lng': -4.469749, 'url': url}
        row = {'pub_name': 'Y Sgwâr (was Ivy Bush)', 'postcode': 'SA38 9BG'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': command['lat'], 'lng': command['lng'], 'postcode': 'SA38 9BG',
                  'address': 'Emlyn Square', 'name': 'Y Sgwâr, Newcastle Emlyn - Pub with Beer Garden'}
        page = '<h1>Y Sgwâr, Newcastle Emlyn (Square)</h1><dt>Previous Names</dt><dd>Ivy Bush</dd>'
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        for bad in (page.replace('Ivy Bush', 'Other'), page.replace('<h1>Y Sgwâr', '<h1>Other')):
            with self.assertRaises(ValueError):
                validate_retained_point(command, row, entry, source, bad)
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, name='Other, Newcastle Emlyn'), page)

    def test_retained_club_ignores_pmc_designation_but_requires_name(self):
        url = 'https://camra.org.uk/pubs/131826'
        command = {'lat': 52.61053543, 'lng': -1.63121538, 'url': url}
        row = {'pub_name': 'Working Mens Club (PMC)', 'postcode': 'B78 1AF'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': 52.610551470899, 'lng': -1.631239708004, 'postcode': 'B78 1AF',
                  'address': '31 New Street', 'name': 'Birchmoor Working Mens Club, Birchmoor'}
        self.assertEqual(validate_retained_point(command, row, entry, source), entry['verified_pin'])
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, name='Birchmoor Social Club'))

    def test_retained_slash_names_accept_reverse_order_with_exact_locality(self):
        url = 'https://camra.org.uk/pubs/115768'
        command = {'lat': 53.0829, 'lng': -3.7695, 'url': url}
        row = {'pub_name': 'White Horse Inn / Tafarn Penllan', 'place_name': 'Capel Garmon', 'postcode': 'LL26 0RW'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': command['lat'], 'lng': command['lng'], 'postcode': 'LL26 0RW',
                  'address': '', 'locality': 'Capel Garmon', 'name': 'Tafarn Penllan, Capel Garmon - Pub with Stove'}
        page = '<h1>Tafarn Penllan, Capel Garmon (White Horse Inn)</h1>'
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, locality='Other Village'), page)
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, source, '<h1>Other Pub, Capel Garmon (White Horse Inn)</h1>')
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, source, '<h1>Tafarn Penllan, Capel Garmon (Other Inn)</h1>')

    def test_retained_point_accepts_only_exact_omitted_arms_suffix(self):
        url = 'https://camra.org.uk/pubs/188280'
        command = {'lat': 51.2066561, 'lng': -4.1257507, 'url': url}
        row = {'pub_name': 'Wellington', 'postcode': 'EX34 9QE'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': 51.2067224, 'lng': -4.1256102, 'postcode': 'EX34 9QE',
                  'address': '66-67 High Street', 'name': 'Wellington Arms, Ilfracombe - Town Local'}
        page = '<h1>Wellington Arms, Ilfracombe</h1>'
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        for bad_source, bad_page in ((dict(source, name='Other Wellington Arms'), page),
                                     (source, '<h1>Wellington Hotel, Ilfracombe</h1>')):
            with self.assertRaises(ValueError):
                validate_retained_point(command, row, entry, bad_source, bad_page)

    def test_retained_slash_names_require_exact_camra_heading_alias(self):
        url = 'https://camra.org.uk/pubs/143292'
        command = {'lat': 52.921261, 'lng': -1.404047, 'url': url}
        row = {'pub_name': 'Village/Conservative Club (PMC)', 'place_name': 'Derby: Spondon', 'postcode': 'DE21 7JP'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': command['lat'], 'lng': command['lng'], 'postcode': 'DE21 7JP',
                  'address': '13 Chapel Street', 'name': 'Village Club, Derby - Club with Live Music'}
        page = '<h1>Village Club, Derby (Spondon Conservative Club)</h1>'
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, source, '<h1>Village Club, Derby (Spondon Liberal Club)</h1>')
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, name='Other Club, Derby'), page)

    def test_working_mens_club_abbreviation_requires_full_named_match(self):
        listing = 'Tynemouth WMC/Social Club (PMC)'
        self.assertTrue(manual_name_matches(listing, 'Tynemouth & District Working Mens Social Club & Institute Ltd'))
        self.assertFalse(manual_name_matches(listing, 'Tynemouth District Social Club'))
        self.assertFalse(manual_name_matches(listing, 'Whitley Bay Working Mens Social Club'))

    def test_retained_point_accepts_exact_fsa_name_address_and_point(self):
        url = 'https://camra.org.uk/pubs/122990'
        command = {'lat': 52.965468, 'lng': -2.1813, 'url': url, 'fsa_id': 113675}
        row = {'pub_name': 'Toby Carvery Trentham Village', 'postcode': 'ST4 8BU'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': 52.965468, 'lng': -2.1813, 'postcode': 'ST4 8BU',
                  'address': '206 Longton Road', 'name': 'Toby Carvery, Stoke-on-Trent'}
        fsa = {'FHRSID': 113675, 'BusinessName': 'Toby Carvery Trentham Village', 'AddressLine1': '',
               'AddressLine2': 'Longton Road', 'PostCode': 'ST4 8BU',
               'geocode': {'latitude': '52.965468', 'longitude': '-2.1813'}}
        self.assertEqual(validate_retained_point(command, row, entry, source, '', fsa), entry['verified_pin'])
        for change in ({'BusinessName': 'Toby Carvery Other'}, {'PostCode': 'ST4 1AA'},
                       {'AddressLine2': 'Other Road'}, {'geocode': {'latitude': '53.1', 'longitude': '-2.1'}}):
            with self.assertRaises(ValueError):
                validate_retained_point(command, row, entry, source, '', dict(fsa, **change))

    def test_camra_heading_requires_complete_exact_alias(self):
        page = '<h1>Eden Park, Eden Park (Toby Carvery)</h1>'
        self.assertTrue(camra_heading_matches_listing('Toby Carvery', page))
        self.assertFalse(camra_heading_matches_listing('Toby Carvery Other', page))
        self.assertFalse(camra_heading_matches_listing('Toby Carvery', '<h1>Eden Park</h1><p>Toby Carvery</p>'))

    def test_retained_point_requires_exact_fsa_premises_and_camra_name(self):
        url = 'https://camra.org.uk/pubs/188466'
        command = {'lat': 51.172685037242, 'lng': -4.2054578186163, 'url': url, 'fsa_id': 178413}
        row = {'pub_name': 'Tides Out', 'postcode': 'EX34 7BP'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': command['lat'], 'lng': command['lng'], 'postcode': 'EX34 7BP',
                  'address': 'Beach Road', 'name': 'Tides Inn, Woolacombe'}
        fsa = {'FHRSID': 178413, 'BusinessName': 'The Tides Inn', 'AddressLine1': 'The Tidesout',
               'AddressLine2': 'Beach Road', 'PostCode': 'EX34 7FG'}
        self.assertEqual(validate_retained_point(command, row, entry, source, '', fsa), entry['verified_pin'])
        for change in ({'AddressLine1': 'Different Pub'}, {'AddressLine2': 'Other Street'},
                       {'BusinessName': 'Other Inn'}, {'FHRSID': 1}):
            with self.assertRaises(ValueError):
                validate_retained_point(command, row, entry, source, '', dict(fsa, **change))

    def test_retained_point_accepts_exact_documented_parenthetical_nickname(self):
        url = 'https://camra.org.uk/pubs/171270'
        command = {'lat': 52.740011107146, 'lng': -1.352965300941, 'url': url}
        row = {'pub_name': 'Three Horseshoes (Pollys)', 'postcode': 'LE67 5GN'}
        entry = {'verified_pin': {'lat': command['lat'], 'lng': command['lng'], 'evidence': [{'source': 'camra', 'url': url}]}}
        source = {'lat': command['lat'], 'lng': command['lng'], 'postcode': 'LE67 5GN',
                  'address': '11 Leicester Road', 'name': 'Three Horseshoes, Whitwick - Historic Interior'}
        page = "<h1>Three Horseshoes, Whitwick (Polly's)</h1>"
        self.assertEqual(validate_retained_point(command, row, entry, source, page), entry['verified_pin'])
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, source, '<h1>Three Horseshoes, Whitwick (Other)</h1>')
        with self.assertRaises(ValueError):
            validate_retained_point(command, row, entry, dict(source, name='Other Horseshoes'), page)

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


