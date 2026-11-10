import csv
import copy
import json
import os
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Browser smoke tests run in CI")
class FrontendSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        os.chdir(cls.repo_root)
        cls.data_root = (cls.repo_root / os.environ.get('DIRECTORY_TEST_ROOT', '.')).resolve()
        cls.release_override = None

        class Handler(SimpleHTTPRequestHandler):
            def translate_path(self, path):
                basename = urlsplit(path).path.lstrip('/')
                if basename in ('directory-release.json', 'pubs.csv'):
                    return str(cls.data_root / basename)
                return super().translate_path(path)

            def do_GET(self):
                if urlsplit(self.path).path == '/directory-release.json' and cls.release_override is not None:
                    content = json.dumps(cls.release_override).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                else:
                    super().do_GET()

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1440,1000")
        cls.driver = webdriver.Chrome(options=options)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()

    def test_directory_loads_and_nearby_defaults_to_permanent(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self.driver.get(self.base_url)
        wait = WebDriverWait(self.driver, 40)
        wait.until(lambda driver: "hidden" in driver.find_element(By.ID, "loadingSplash").get_attribute("class"))
        with (self.data_root / "pubs.csv").open(encoding="utf-8", newline="") as handle:
            expected_count = sum(1 for _ in csv.DictReader(handle))
        self.assertEqual(self.driver.find_element(By.ID, "resultCount").text, f"{expected_count:,} matches")
        self.driver.execute_script("setUserLocation(51.5, -0.1, 'Test location');")
        self.assertEqual(self.driver.find_element(By.ID, "typeFilter").get_attribute("value"), "Perm")

    def load_directory(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        self.driver.get(self.base_url)
        WebDriverWait(self.driver, 40).until(lambda driver: 'hidden' in driver.find_element(By.ID, 'loadingSplash').get_attribute('class'))

    def tearDown(self):
        type(self).release_override = None

    def save_review_screenshot(self, name):
        directory = os.environ.get('DIRECTORY_TEST_SCREENSHOTS')
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            self.driver.save_screenshot(str(path / name))

    def resize_viewport(self, width, height):
        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False,
        })

    def test_muckers_keeps_verified_pin_and_directions_despite_postcode_conflict(self):
        self.load_directory()
        result = self.driver.execute_script("const p = pubData.find(p => p.pub_name === 'GJ Muckers'); return {precision:p.locationPrecision, lat:p.lat, lng:p.lng, issue:p.locationIssue, directions:directionsUrl(p)};")
        self.assertEqual(result['precision'], 'venue')
        self.assertAlmostEqual(result['lat'], 53.25777)
        self.assertAlmostEqual(result['lng'], -2.12237)
        self.assertEqual(result['issue']['reference'], 'SK11 6JL')
        self.assertIn('destination=53.25777', result['directions'])
        self.assertNotIn('SK11', result['directions'])

    def test_black_lion_is_one_listing(self):
        self.load_directory()
        result = self.driver.execute_script("return pubData.filter(p => p.pub_name === 'Black Lion' && p.place_name === 'Blackfordby').map(p => p.pg)")
        self.assertEqual(result, ['Guest'])

    def test_nearby_status_tracks_guest_all_and_radius_filters(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select
        self.load_directory()
        Select(self.driver.find_element(By.ID, 'typeFilter')).select_by_value('Guest')
        self.driver.execute_script("setUserLocation(51.45593, -2.593993, 'Test postcode');")
        self.assertIn('closest guest listings within 25 miles', self.driver.find_element(By.ID, 'locationStatus').text)
        self.assertNotIn('permanent', self.driver.find_element(By.ID, 'locationStatus').text)
        self.save_review_screenshot('Bass Map nearby Guest preview.png')
        Select(self.driver.find_element(By.ID, 'typeFilter')).select_by_value('')
        Select(self.driver.find_element(By.ID, 'radiusFilter')).select_by_value('')
        self.assertIn('closest all listings at any distance', self.driver.find_element(By.ID, 'locationStatus').text)
        Select(self.driver.find_element(By.ID, 'typeFilter')).select_by_value('Perm')
        Select(self.driver.find_element(By.ID, 'radiusFilter')).select_by_value('10')
        self.assertIn('closest permanent listings within 10 miles', self.driver.find_element(By.ID, 'locationStatus').text)

    def test_report_dialog_keeps_keyboard_focus_and_restores_background(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        self.load_directory()
        # This test exercises only local modal behavior. No report service or
        # challenge request is needed and the form is never submitted.
        self.driver.execute_script('loadTurnstile = async () => {};')
        self.driver.find_element(By.ID, 'openProblemReportBtn').click()
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'problemMessage')
        self.assertTrue(self.driver.execute_script("return document.querySelector('.layout').inert"))
        self.driver.find_element(By.ID, 'submitProblemReportBtn').send_keys(Keys.TAB)
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'closeProblemReportBtn')
        self.driver.switch_to.active_element.send_keys(Keys.SHIFT, Keys.TAB)
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'submitProblemReportBtn')
        self.driver.set_window_size(390, 844)
        self.save_review_screenshot('Bass Map report dialog preview.png')
        self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        self.assertTrue(self.driver.execute_script('return document.getElementById("reportDialog").hidden'))
        self.assertFalse(self.driver.execute_script("return document.querySelector('.layout').inert"))
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'openProblemReportBtn')
        self.driver.set_window_size(1440, 1000)

    def test_mobile_venue_details_fit_and_restore_focus(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver, 10)

        def reachable(element):
            return driver.execute_script("""
                const element = arguments[0];
                const {left, top, right, bottom, width, height} = element.getBoundingClientRect();
                const dialog = element.closest('dialog').getBoundingClientRect();
                return left >= dialog.left && top >= dialog.top && right <= dialog.right && bottom <= dialog.bottom
                    && element.contains(document.elementFromPoint(left + width / 2, top + height / 2));
            """, element)

        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.clearDeviceMetricsOverride', {})
        self.resize_viewport(360, 640)
        self.load_directory()
        driver.find_element(By.ID, 'openPanelBtn').click()
        search = wait.until(EC.element_to_be_clickable((By.ID, 'searchBox')))
        search.send_keys('Bank Tavern')
        wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
        result = driver.find_element(By.CSS_SELECTOR, '#results .card-map-button')
        result_directions = driver.find_element(By.CSS_SELECTOR, '#results .card-actions a').get_attribute('href')
        driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", result)
        scroll_position = driver.execute_script("return document.querySelector('.sidebar-inner').scrollTop;")
        result.send_keys(Keys.ENTER)

        close = wait.until(EC.visibility_of_element_located((By.ID, 'closeMobileVenueBtn')))
        title = driver.find_element(By.ID, 'mobileVenueTitle')
        directions = driver.find_element(By.LINK_TEXT, 'Get directions')
        self.assertEqual(title.text, 'Bank Tavern')
        self.assertLess(title.size['height'], 40)
        self.assertEqual(close, driver.switch_to.active_element)
        self.assertEqual(directions.get_attribute('href'), result_directions)
        self.assertTrue(reachable(directions))
        wait.until(lambda d: d.execute_script(
            "return document.getElementById('sidebarPanel').getBoundingClientRect().top >= innerHeight;"
        ))
        self.save_review_screenshot('mobile-venue-360x640.png')

        for width, height in [(360, 640), (640, 360), (430, 932), (768, 1024)]:
            with self.subTest(width=width, height=height):
                self.resize_viewport(width, height)
                self.assertTrue(reachable(title))
                self.assertTrue(reachable(close))
                if width == 640:
                    self.assertTrue(reachable(directions))
                    self.save_review_screenshot('mobile-venue-640x360.png')
                driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", directions)
                self.assertTrue(reachable(directions))

        self.resize_viewport(360, 640)
        position = "return [map.getCenter().lat, map.getCenter().lng, map.getZoom()];"
        centre = driver.execute_script(position)
        close.click()
        wait.until(lambda d: result == d.switch_to.active_element)
        self.assertEqual(search.get_attribute('value'), 'Bank Tavern')
        self.assertEqual(driver.execute_script(position), centre)
        self.assertEqual(driver.execute_script("return document.querySelector('.sidebar-inner').scrollTop;"), scroll_position)

        result.send_keys(Keys.ESCAPE)
        pin = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '#map .leaflet-marker-icon')))
        pin.send_keys(Keys.ENTER)
        wait.until(EC.visibility_of(close))
        close.send_keys(Keys.ESCAPE)
        wait.until(lambda d: pin == d.switch_to.active_element)

        driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
        wait.until(lambda d: d.execute_script('return !isMobileView();'))
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '#results .card-map-button'))).click()
        self.assertEqual(wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.leaflet-popup h3'))).text, 'Bank Tavern')

    def test_mobile_search_shows_results_before_secondary_filters(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import Select, WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver, 10)

        def by_id(element_id):
            return driver.find_element(By.ID, element_id)

        def in_view(element):
            return driver.execute_script("""
                const rect = arguments[0].getBoundingClientRect();
                return rect.top >= 0 && rect.bottom <= innerHeight
                    && arguments[0].contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
            """, element)

        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.clearDeviceMetricsOverride', {})
        self.resize_viewport(360, 640)
        self.load_directory()
        by_id('openPanelBtn').click()
        search = by_id('searchBox')
        search.send_keys('Bank Tavern')
        wait.until(lambda d: by_id('resultCount').text == '1 match')
        title = driver.find_element(By.CSS_SELECTOR, '#results .card-title')
        self.assertEqual(title.text, 'Bank Tavern')
        wait.until(lambda d: in_view(title))
        self.assertTrue(in_view(driver.find_element(By.CSS_SELECTOR, '#results .chip.perm')))
        self.assertFalse(by_id('countryFilter').is_displayed())
        self.save_review_screenshot('mobile-search-360x640.png')

        toggle = by_id('mobileFiltersBtn')
        toggle.click()
        self.assertEqual(toggle.get_attribute('aria-expanded'), 'true')
        self.assertEqual(search.get_attribute('value'), 'Bank Tavern')
        Select(by_id('typeFilter')).select_by_value('Perm')
        self.assertEqual(by_id('openPanelBtn').get_attribute('aria-expanded'), 'true')
        by_id('mobileShowResultsBtn').click()
        self.assertEqual(toggle.get_attribute('aria-expanded'), 'false')
        self.assertEqual(by_id('activeFilterSummary').text, 'Permanent')
        toggle.click()
        self.assertEqual(by_id('typeFilter').get_attribute('value'), 'Perm')
        toggle.click()
        by_id('clearMobileFiltersBtn').click()
        self.assertEqual(search.get_attribute('value'), 'Bank Tavern')
        self.assertEqual(by_id('resultCount').text, '1 match')
        self.assertFalse(by_id('mobileActiveFilters').is_displayed())

        self.resize_viewport(640, 360)
        wait.until(lambda d: in_view(driver.find_element(By.CSS_SELECTOR, '#results .card-actions a')))
        self.save_review_screenshot('mobile-search-640x360.png')
        by_id('closePanelBtn').click()
        wait.until(lambda d: d.execute_script("return document.getElementById('sidebarPanel').getBoundingClientRect().top >= innerHeight;"))
        self.assertLessEqual(driver.find_element(By.CSS_SELECTOR, '.topbar').size['height'], 64)
        self.assertGreaterEqual(by_id('map').size['height'], 290)
        self.assertTrue(in_view(by_id('openProblemReportBtn')))
        self.save_review_screenshot('mobile-map-640x360.png')

        driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
        wait.until(EC.visibility_of_element_located((By.ID, 'countryFilter')))
        self.assertFalse(toggle.is_displayed())
        self.assertEqual(search.get_attribute('value'), 'Bank Tavern')

    def test_expired_evidence_falls_back_to_approximate_pin(self):
        payload = json.loads((self.data_root / 'directory-release.json').read_text(encoding='utf-8'))
        row = next(r for r in payload['rows'] if r['pub_name'] == 'GJ Muckers')
        payload['locations'][row['venue_id']]['checked_at'] = '2000-01-01'
        type(self).release_override = payload
        self.load_directory()
        precision = self.driver.execute_script("return pubData.find(p => p.pub_name === 'GJ Muckers').locationPrecision")
        self.assertEqual(precision, 'postcode')

    def test_corrupt_release_does_not_render_a_wrong_pin(self):
        payload = json.loads((self.data_root / 'directory-release.json').read_text(encoding='utf-8'))
        payload['locations'][payload['rows'][0]['venue_id']]['lat'] = 99
        type(self).release_override = payload
        self.load_directory()
        self.assertEqual(self.driver.execute_script('return pubData.length'), 0)
