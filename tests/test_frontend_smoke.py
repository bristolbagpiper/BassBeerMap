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
