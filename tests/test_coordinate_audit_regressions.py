import unittest
from resolve_venue_coordinates_from_osm import choose_match

class ConcaveBuildingTests(unittest.TestCase):
    def test_closed_way_marker_is_in_the_building(self):
        from resolve_venue_coordinates_from_osm import interior_point
        # The vertex mean of this U-shaped pub falls in its open courtyard.
        ring = [(0,0),(0,6),(4,6),(4,5),(1,5),(1,1),(4,1),(4,0),(0,0)]
        y, x = interior_point(ring)
        self.assertTrue(0 < y < 4 and 0 < x < 6 and (y < 1 or x < 1 or x > 5))
        self.assertIsNone(interior_point(ring[:-1]))
        self.assertIsNone(interior_point([(1,1)] * 4))


class CoordinateAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.row={'pub_name':'Crown','place_name':'Example Village','postcode':'AA1 1AA'}
        self.postcodes={'AA1 1AA':{'lat':52.0,'lng':-1.0}}
    def candidate(self,lat=52.001,postcode='AA1 1AA'):
        return {'name':'Crown','lat':lat,'lng':-1.0,'postcode':postcode,'place':'Example Village'}
    def test_known_conflicting_postcode_cannot_be_overruled_by_proximity(self):
        self.assertIsNone(choose_match(self.row,[self.candidate(52.035,'AA9 9ZZ')],self.postcodes))
    def test_two_different_locations_at_same_postcode_require_review(self):
        self.assertIsNone(choose_match(self.row,[self.candidate(),self.candidate(52.005)],self.postcodes))
    def test_node_and_building_of_same_pub_can_agree(self):
        self.assertIsNotNone(choose_match(self.row,[self.candidate(),self.candidate(52.0011)],self.postcodes))
    def test_exact_address_still_matches(self):
        self.assertIsNotNone(choose_match(self.row,[self.candidate()],self.postcodes))
