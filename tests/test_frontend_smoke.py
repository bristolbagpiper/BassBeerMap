import csv
import copy
import json
import os
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Browser smoke tests run in CI")
class FrontendSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        os.chdir(cls.repo_root)
        cls.data_root = (cls.repo_root / os.environ.get('DIRECTORY_TEST_ROOT', '.')).resolve()
        cls.release_override = None
        cls.received_reports = []

        class Handler(SimpleHTTPRequestHandler):
            def translate_path(self, path):
                basename = urlsplit(path).path.lstrip('/')
                if basename in ('directory-release.json', 'pubs.csv'):
                    return str(cls.data_root / basename)
                return super().translate_path(path)

            def do_GET(self):
                if self.path == '/api/report-problem?config=1':
                    self.send_json({'turnstileSiteKey': 'local-test-key'})
                elif urlsplit(self.path).path == '/directory-release.json' and cls.release_override is not None:
                    self.send_json(cls.release_override)
                else:
                    super().do_GET()

            def do_POST(self):
                if self.path != '/api/report-problem':
                    self.send_error(404)
                    return
                cls.received_reports.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_json({'ok': True})

            def send_json(self, data):
                content = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

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
        type(self).received_reports.clear()

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

    def open_result_pin_details(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        self.driver.find_element(By.CSS_SELECTOR, '#results .card-map-button').click()
        wait = WebDriverWait(self.driver, 10)
        wait.until(EC.visibility_of_element_located((By.ID, 'mobileVenue')))
        return wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '#map .leaflet-marker-icon[role="button"]')))

    def test_muckers_opens_verified_pin_despite_postcode_conflict(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self.load_directory()
        result = self.driver.execute_script("const p = pubData.find(p => p.pub_name === 'GJ Muckers'); return {precision:p.locationPrecision, lat:p.lat, lng:p.lng, issue:p.locationIssue};")
        self.assertEqual(result['precision'], 'venue')
        self.assertAlmostEqual(result['lat'], 53.25777)
        self.assertAlmostEqual(result['lng'], -2.12237)
        self.assertEqual(result['issue']['reference'], 'SK11 6JL')
        self.driver.find_element(By.ID, 'searchBox').send_keys('GJ Muckers')
        WebDriverWait(self.driver, 10).until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
        card = self.driver.find_element(By.CSS_SELECTOR, '#results .card')
        links = card.find_elements(By.TAG_NAME, 'a')
        self.assertEqual([link.text for link in links], ['Open in Google Maps'])
        pin_url = urlsplit(links[0].get_attribute('href'))
        self.assertEqual(pin_url.path, '/maps/search/')
        self.assertEqual(parse_qs(pin_url.query), {'api': ['1'], 'query': ['53.25777,-2.12237']})

    def test_one_google_maps_link_uses_verified_address_in_results_and_popups(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        venues = [
            ('Bank Tavern', 'Bank Tavern, 8 John Street, Bristol, BS1 2HR'),
            ('Brew Tavern', "Brew Tavern, Cabman's Shelter, 1a Queens Road, Nottingham, NG2 3AS"),
            ('Anchor Newhall', 'Anchor, 211 High Street, Newhall, DE11 0EA'),
            ('Anchor Teanford', 'Anchor, Breach Lane, Teanford, ST10 4EW'),
            ('Old Down Inn Emborough', 'Old Down Inn, Emborough, BA3 4SA'),
        ]
        driver = self.driver
        wait = WebDriverWait(driver, 10)
        for search, address in venues:
            with self.subTest(venue=search):
                self.load_directory()
                driver.find_element(By.ID, 'searchBox').send_keys(search)
                wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
                card = driver.find_element(By.CSS_SELECTOR, '#results .card')
                links = card.find_elements(By.TAG_NAME, 'a')
                self.assertEqual([link.text for link in links], ['Open in Google Maps'])
                url = links[0].get_attribute('href')
                parts = urlsplit(url)
                self.assertEqual((parts.scheme, parts.netloc, parts.path), ('https', 'www.google.com', '/maps/search/'))
                self.assertEqual(parse_qs(parts.query), {'api': ['1'], 'query': [address]})
                self.open_result_pin_details()
                popup = wait.until(EC.visibility_of_element_located((By.ID, 'mobileVenue')))
                popup_links = popup.find_elements(By.TAG_NAME, 'a')
                self.assertEqual([link.text for link in popup_links], ['Open in Google Maps'])
                self.assertEqual(popup_links[0].get_attribute('href'), url)

    def test_black_lion_is_one_listing(self):
        self.load_directory()
        result = self.driver.execute_script("return pubData.filter(p => p.pub_name === 'Black Lion' && p.place_name === 'Blackfordby').map(p => p.pg)")
        self.assertEqual(result, ['Guest'])

    def test_nearby_scope_keeps_radius_and_availability_independent(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select
        self.load_directory()
        driver = self.driver
        availability = Select(driver.find_element(By.ID, 'typeFilter'))
        availability.select_by_value('Guest')
        driver.execute_script("setUserLocation(51.45593, -2.593993, 'Nearby BS1 2HR');")
        scope = driver.find_element(By.ID, 'nearbyScope')
        radius = Select(scope.find_element(By.ID, 'radiusFilter'))
        self.assertEqual(scope.find_element(By.ID, 'nearbyOrigin').text, 'Nearby BS1 2HR')
        self.assertEqual(availability.first_selected_option.text, 'Guest')
        self.assertEqual(radius.first_selected_option.text, 'Within 25 miles')
        radius.select_by_value('')
        self.assertEqual(availability.first_selected_option.text, 'Guest')
        self.assertEqual(radius.first_selected_option.text, 'Any distance')
        availability.select_by_value('')
        self.assertEqual(radius.first_selected_option.text, 'Any distance')
        self.assertEqual(availability.first_selected_option.text, 'All types')
        radius.select_by_value('10')
        availability.select_by_value('Perm')
        self.assertEqual(radius.first_selected_option.text, 'Within 10 miles')
        self.assertEqual(availability.first_selected_option.text, 'Permanent')

    def test_mobile_search_all_listings_keeps_the_pub_name(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import Select, WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver, 10)

        def by_id(element_id):
            return driver.find_element(By.ID, element_id)

        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.clearDeviceMetricsOverride', {})
        self.resize_viewport(360, 640)
        self.load_directory()
        driver.execute_script("""
            const fetchFromNetwork = window.fetch;
            window.fetch = (url, ...options) => {
                if (url === 'https://api.postcodes.io/postcodes/BS1%205AH') {
                    const response = Response.json({
                        result: {postcode: 'BS1 5AH', latitude: 51.4532, longitude: -2.6008}
                    });
                    if (window.delayTestPostcode) {
                        return new Promise(resolve => { window.finishTestPostcode = () => resolve(response); });
                    }
                    return Promise.resolve(response);
                }
                if (url === 'https://api.postcodes.io/postcodes/ZZ99%209ZZ') {
                    return Promise.resolve(new Response(null, {status: 404}));
                }
                return fetchFromNetwork(url, ...options);
            };
        """)
        by_id('openPanelBtn').click()
        search = by_id('searchBox')
        wait.until(lambda d: 'full UK postcode to search nearby' in by_id('searchHelp').text)
        search.send_keys('BS1 5AH', Keys.ENTER)
        wait.until(lambda d: by_id('nearbyOrigin').text == 'Nearby BS1 5AH')
        scope = by_id('nearbyScope')
        radius = Select(scope.find_element(By.ID, 'radiusFilter'))
        availability = Select(scope.find_element(By.ID, 'typeFilter'))
        self.assertEqual(radius.first_selected_option.text, 'Within 25 miles')
        self.assertEqual(availability.first_selected_option.text, 'Permanent')

        search.send_keys('Brew Tavern')
        wait.until(lambda d: by_id('resultCount').text == '0 matches')
        self.assertIn('Nothing within 25 miles', by_id('results').text)
        self.assertIn('Brew Tavern in Nottingham', by_id('results').text)
        self.save_review_screenshot('nearby-brew-tavern-360x640.png')
        driver.find_element(By.XPATH, "//button[normalize-space()='Show closest listings']").click()
        self.assertEqual(radius.first_selected_option.text, 'Any distance')
        self.assertEqual(availability.first_selected_option.text, 'Permanent')
        self.assertIn('miles away', by_id('results').text)
        radius.select_by_value('25')
        by_id('searchAllListingsBtn').click()
        self.assertEqual(search.get_attribute('value'), 'Brew Tavern')
        self.assertEqual(by_id('resultCount').text, '1 match')
        self.assertIn('Brew Tavern', by_id('results').text)
        self.assertFalse(scope.is_displayed())
        self.assertEqual(by_id('typeFilter').get_attribute('value'), '')
        self.assertEqual(driver.switch_to.active_element, by_id('resultsHeading'))
        self.save_review_screenshot('all-listings-brew-tavern-360x640.png')

        search.send_keys(Keys.CONTROL, 'a', Keys.NULL, 'ZZ99 9ZZ', Keys.ENTER)
        wait.until(lambda d: 'That postcode could not be located' in by_id('locationStatus').text)
        self.assertEqual(search.get_attribute('value'), 'ZZ99 9ZZ')
        search.send_keys(Keys.CONTROL, 'a', Keys.NULL, 'Bank Tavern')
        wait.until(lambda d: by_id('resultCount').text == '1 match')
        self.assertIn('Bank Tavern', by_id('results').text)
        self.assertEqual(by_id('locationStatus').text, '')

        driver.execute_script('window.delayTestPostcode = true;')
        search.send_keys(Keys.CONTROL, 'a', Keys.NULL, 'BS1 5AH', Keys.ENTER)
        wait.until(lambda d: 'Finding that postcode' in by_id('locationStatus').text)
        search.send_keys(Keys.CONTROL, 'a', Keys.NULL, 'Brew Tavern')
        driver.execute_script('window.finishTestPostcode();')
        wait.until(lambda d: 'Brew Tavern' in by_id('results').text)
        self.assertEqual(search.get_attribute('value'), 'Brew Tavern')
        self.assertFalse(scope.is_displayed())

    def test_report_dialog_keeps_keyboard_focus_and_restores_background(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        self.load_directory()
        # This test exercises only local modal behavior. No report service or
        # challenge request is needed and the form is never submitted.
        self.driver.execute_script('loadTurnstile = async () => {};')
        opener = self.driver.find_element(By.ID, 'openProblemReportBtn')
        opener.click()
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'reportDialogTitle')
        self.assertTrue(self.driver.execute_script("return document.getElementById('reportDialog').matches(':modal')"))
        self.driver.execute_script("document.getElementById('searchBox').focus()")
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'reportDialogTitle')
        routes = self.driver.find_element(By.ID, 'reportRoute')
        self.assertIn('misplaced pin, wrong directions', routes.text)
        self.assertIn('pub additions or directory corrections, including Bass availability and closures', routes.text)
        self.assertEqual(routes.find_element(By.LINK_TEXT, 'National Bass Directory').get_attribute('href'),
                         'https://nationalbassdirectory.wordpress.com/')
        self.assertEqual(routes.find_element(By.LINK_TEXT, 'Great Stuff This Bass group').get_attribute('href'),
                         'https://www.facebook.com/groups/nationalbassday/')
        self.driver.find_element(By.ID, 'submitProblemReportBtn').send_keys(Keys.TAB)
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'closeProblemReportBtn')
        self.driver.switch_to.active_element.send_keys(Keys.SHIFT, Keys.TAB)
        self.assertEqual(self.driver.switch_to.active_element.get_attribute('id'), 'submitProblemReportBtn')
        self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        self.assertFalse(self.driver.find_element(By.ID, 'reportDialog').is_displayed())
        self.assertEqual(self.driver.switch_to.active_element, opener)

    def test_listing_report_keeps_context_and_submits_only_to_local_receiver(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver, 10)
        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.clearDeviceMetricsOverride', {})
        self.load_directory()
        driver.execute_script("""
            const originalFetch = window.fetch.bind(window);
            window.fetch = (input, options) => {
                const url = new URL(input, location.href);
                if (url.pathname === '/api/report-problem') {
                    return originalFetch(url.pathname + url.search, options);
                }
                return originalFetch(input, options);
            };
            window.turnstile = {
                render: (container, options) => { options.callback('local-test-token'); return 0; },
                reset: () => {}
            };
        """)
        search = driver.find_element(By.ID, 'searchBox')
        search.send_keys('Bank Tavern')
        wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
        venue = driver.find_element(By.ID, 'problemVenue')
        cancel = driver.find_element(By.ID, 'cancelProblemReportBtn')
        context = 'Bank Tavern, Bristol: Central, BS1 2HR'
        card_report = driver.find_element(By.CSS_SELECTOR, '#results .venue-report-btn')
        card_report.click()
        self.assertEqual(venue.get_property('value'), context)
        self.assertTrue(venue.get_property('readOnly'))
        cancel.click()
        self.assertEqual(driver.switch_to.active_element, card_report)
        pin = self.open_result_pin_details()
        pin.send_keys(Keys.ENTER)
        desktop_report = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.leaflet-popup .venue-report-btn')))
        desktop_report.click()
        self.assertEqual(venue.get_property('value'), context)
        driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        self.assertEqual(driver.switch_to.active_element, desktop_report)

        desktop_report.click()
        self.resize_viewport(360, 640)
        wait.until(lambda d: d.execute_script('return isMobileView();'))
        self.assertTrue(driver.find_element(By.ID, 'reportDialog').get_property('open'))
        self.assertEqual(venue.get_property('value'), context)
        cancel.click()
        mobile_report = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '#mobileVenue .venue-report-btn')))
        self.assertEqual(driver.switch_to.active_element, mobile_report)
        mobile_report.click()
        self.assertEqual(venue.get_property('value'), context)
        self.assertTrue(driver.find_element(By.ID, 'mobileVenue').get_property('open'))
        self.save_review_screenshot('venue-report-360x640.png')
        cancel.click()
        self.assertEqual(driver.switch_to.active_element, mobile_report)
        mobile_report.click()
        driver.find_element(By.ID, 'problemMessage').send_keys('Local verification of the map report.')
        driver.find_element(By.ID, 'submitProblemReportBtn').click()
        wait.until(EC.text_to_be_present_in_element((By.ID, 'problemReportStatus'), 'your report has been sent'))
        self.assertEqual(self.received_reports, [{
            'venue': context, 'message': 'Local verification of the map report.', 'contact': '',
            'website': '', 'turnstileToken': 'local-test-token', 'page': self.base_url,
        }])
        self.assertEqual(venue.get_property('value'), context)
        cancel.click()
        driver.find_element(By.ID, 'closeMobileVenueBtn').click()
        wait.until(lambda d: d.switch_to.active_element == pin)
        wait.until(EC.element_to_be_clickable((By.ID, 'openProblemReportBtn'))).click()
        self.assertEqual(venue.get_property('value'), '')
        self.assertFalse(venue.get_property('readOnly'))
        cancel.click()

    def test_confirmation_period_and_availability_are_readable_on_phone(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver, 10)
        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.clearDeviceMetricsOverride', {})
        self.resize_viewport(360, 640)
        for name, availability in [('Bank Tavern', 'Permanent'), ('Black Lion Blackfordby', 'Guest / occasional')]:
            with self.subTest(venue=name):
                self.load_directory()
                driver.find_element(By.ID, 'openPanelBtn').click()
                driver.find_element(By.ID, 'searchBox').send_keys(name)
                wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
                card = driver.find_element(By.CSS_SELECTOR, '#results .card')
                self.assertIn(availability, card.text)
                confirmation = card.find_element(By.CLASS_NAME, 'card-confirmation')
                period = confirmation.text.removeprefix('Last confirmed:').strip()
                self.assertRegex(period, r'^[A-Z][a-z]{2} to [A-Z][a-z]{2} 20\d{2}$')
                if availability == 'Guest / occasional':
                    self.save_review_screenshot('guest-confirmation-360x640.png')
                self.open_result_pin_details()
                details = wait.until(EC.visibility_of_element_located((By.ID, 'mobileVenueContent')))
                self.assertIn(f'Availability: {availability}', details.text)
                detail_confirmation = details.find_element(By.CLASS_NAME, 'venue-confirmation')
                self.assertIn(period, detail_confirmation.text)
                driver.execute_script("document.documentElement.style.fontSize = '200%'")
                for element in [confirmation, detail_confirmation]:
                    self.assertTrue(driver.execute_script("""
                        const element = arguments[0];
                        return parseFloat(getComputedStyle(element).fontSize) >= 20
                            && element.scrollWidth <= element.clientWidth + 1;
                    """, element))
                driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'})", detail_confirmation)
                self.save_review_screenshot('venue-confirmation-200-percent.png')

    def test_view_on_map_centres_the_pin_and_leaves_the_map_free_to_pan(self):
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.interaction import POINTER_TOUCH
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver, 10)
        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.clearDeviceMetricsOverride', {})
        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.setTouchEmulationEnabled', {'enabled': False})
        driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {'enabled': True})
        for width, height in [(360, 640), (640, 360)]:
            with self.subTest(width=width, height=height):
                self.resize_viewport(width, height)
                self.load_directory()
                driver.find_element(By.ID, 'openPanelBtn').click()
                search = driver.find_element(By.ID, 'searchBox')
                search.send_keys('Bank Tavern')
                wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
                self.save_review_screenshot(f'map-result-{width}x{height}.png')
                pin = self.open_result_pin_details()
                self.assertEqual(pin.accessible_name, 'Bank Tavern, Bristol: Central')
                wait.until(lambda d: d.execute_script(
                    "return document.getElementById('sidebarPanel').getBoundingClientRect().top >= innerHeight - 1;"))
                self.assertTrue(driver.execute_script("""
                    const mapBox = document.getElementById('map').getBoundingClientRect();
                    const pinBox = arguments[0].getBoundingClientRect();
                    return Math.abs(pinBox.left + pinBox.width / 2 - (mapBox.left + mapBox.width / 2)) <= 2
                        && Math.abs(pinBox.bottom - (mapBox.top + mapBox.height / 2)) <= 2;
                """, pin))
                factbox = driver.find_element(By.ID, 'mobileVenue')
                self.assertEqual(factbox.find_element(By.TAG_NAME, 'h2').text, 'Bank Tavern')
                self.assertTrue(driver.execute_script("""
                    const factbox = arguments[0], pin = arguments[1];
                    const map = document.getElementById('map').getBoundingClientRect();
                    const r = pin.getBoundingClientRect();
                    return !factbox.matches(':modal')
                        && factbox.getBoundingClientRect().width * factbox.getBoundingClientRect().height < map.width * map.height / 2
                        && pin.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
                """, factbox, pin))
                self.assertFalse(driver.find_elements(By.CSS_SELECTOR, '.leaflet-popup'))
                self.save_review_screenshot(f'centred-factbox-{width}x{height}.png')

                position = 'return [map.getCenter().lat, map.getCenter().lng];'
                centre = driver.execute_script(position)
                map_element = driver.find_element(By.ID, 'map')
                touch = ActionBuilder(driver, mouse=PointerInput(POINTER_TOUCH, 'finger'))
                touch.pointer_action.move_to(map_element, -75, -55)
                touch.pointer_action.pointer_down()
                touch.pointer_action.move_by(55, 15)
                touch.pointer_action.pointer_up()
                touch.perform()
                wait.until(lambda d: d.execute_script(position) != centre)
                self.assertTrue(factbox.get_property('open'))
                driver.find_element(By.ID, 'openPanelBtn').click()
                self.assertEqual(search.get_property('value'), 'Bank Tavern')

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
        self.assertTrue(driver.execute_script("return document.querySelector('.map-wrap').inert && document.querySelector('.topbar').inert;"))
        search = wait.until(EC.element_to_be_clickable((By.ID, 'searchBox')))
        search.send_keys('Bank Tavern')
        wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
        result_maps = driver.find_element(By.CSS_SELECTOR, '#results .card-actions a').get_attribute('href')
        pin = self.open_result_pin_details()

        close = wait.until(EC.visibility_of_element_located((By.ID, 'closeMobileVenueBtn')))
        title = driver.find_element(By.ID, 'mobileVenueTitle')
        maps_link = driver.find_element(By.CSS_SELECTOR, '#mobileVenue a[target="_blank"]')
        self.assertEqual(title.text, 'Bank Tavern')
        self.assertLess(title.size['height'], 40)
        self.assertEqual(close, driver.switch_to.active_element)
        self.assertEqual(maps_link.get_attribute('href'), result_maps)
        self.assertTrue(reachable(maps_link))
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
                    self.assertTrue(reachable(maps_link))
                    self.save_review_screenshot('mobile-venue-640x360.png')
                driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'});", maps_link)
                self.assertTrue(reachable(maps_link))

        self.resize_viewport(360, 640)
        position = "return [map.getCenter().lat, map.getCenter().lng, map.getZoom()];"
        centre = driver.execute_script(position)
        close.click()
        wait.until(lambda d: pin == d.switch_to.active_element)
        self.assertEqual(search.get_attribute('value'), 'Bank Tavern')
        self.assertEqual(driver.execute_script(position), centre)
        self.assertFalse(driver.execute_script("return document.querySelector('.map-wrap').inert;"))
        pin.send_keys(Keys.ENTER)
        wait.until(EC.visibility_of(close))
        close.send_keys(Keys.ESCAPE)
        wait.until(lambda d: pin == d.switch_to.active_element)

        driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
        wait.until(lambda d: d.execute_script('return !isMobileView();'))
        self.open_result_pin_details()
        self.assertEqual(title.text, 'Bank Tavern')

    def test_tablet_rotation_preserves_the_open_venue_and_focus(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self.driver
        wait = WebDriverWait(driver, 10)
        self.addCleanup(driver.execute_cdp_cmd, 'Emulation.clearDeviceMetricsOverride', {})
        self.resize_viewport(768, 1024)
        self.load_directory()
        driver.find_element(By.ID, 'openPanelBtn').click()
        search = driver.find_element(By.ID, 'searchBox')
        search.send_keys('Bank Tavern')
        wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
        pin = self.open_result_pin_details()

        dialog = driver.find_element(By.ID, 'mobileVenue')
        wait.until(EC.visibility_of(dialog))
        close = dialog.find_element(By.ID, 'closeMobileVenueBtn')
        maps_link = dialog.find_element(By.LINK_TEXT, 'Open in Google Maps')
        href = maps_link.get_attribute('href')
        close.send_keys(Keys.TAB, Keys.TAB)
        self.assertEqual(driver.switch_to.active_element, maps_link)
        self.save_review_screenshot('tablet-venue-portrait.png')

        for width, height in [(1024, 768), (768, 1024)]:
            with self.subTest(width=width, height=height):
                self.resize_viewport(width, height)
                wait.until(lambda d: d.execute_script('return innerWidth') == width)
                self.assertTrue(dialog.get_property('open'))
                self.assertEqual(dialog.find_element(By.TAG_NAME, 'h2').text, 'Bank Tavern')
                self.assertEqual(maps_link.get_attribute('href'), href)
                self.assertEqual(driver.switch_to.active_element, maps_link)
                self.assertEqual(search.get_property('value'), 'Bank Tavern')
                for control in [close, maps_link]:
                    self.assertTrue(driver.execute_script("""
                        const element = arguments[0];
                        const rect = element.getBoundingClientRect();
                        return element.contains(document.elementFromPoint(
                            rect.left + rect.width / 2, rect.top + rect.height / 2));
                    """, control))
                name = 'landscape' if width > height else 'portrait-return'
                self.save_review_screenshot(f'tablet-venue-{name}.png')

        close.click()
        wait.until(lambda d: d.switch_to.active_element == pin)
        self.assertEqual(search.get_property('value'), 'Bank Tavern')

        self.resize_viewport(1024, 768)
        wait.until(lambda d: d.execute_script('return !isMobileView();'))
        pin = self.open_result_pin_details()
        pin.send_keys(Keys.ENTER)
        desktop_title = (By.CSS_SELECTOR, '.leaflet-popup h3')
        heading = wait.until(EC.visibility_of_element_located(desktop_title))
        self.assertEqual(heading.text, 'Bank Tavern')
        heading.send_keys(Keys.TAB)
        self.assertEqual(driver.switch_to.active_element.text, 'Open in Google Maps')
        self.resize_viewport(768, 1024)
        wait.until(EC.visibility_of(dialog))
        self.assertEqual(dialog.find_element(By.TAG_NAME, 'h2').text, 'Bank Tavern')
        compact_maps_link = dialog.find_element(By.LINK_TEXT, 'Open in Google Maps')
        self.assertEqual(compact_maps_link.get_attribute('href'), href)
        self.assertEqual(driver.switch_to.active_element, compact_maps_link)
        close.click()
        wait.until(lambda d: d.switch_to.active_element == pin)

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

    def test_keyboard_navigation_and_map_control_names(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        self.load_directory()
        driver = self.driver
        wait = WebDriverWait(driver, 10)

        def press(key):
            driver.switch_to.active_element.send_keys(key)

        press(Keys.TAB)
        self.assertEqual(driver.switch_to.active_element.accessible_name, 'Skip to pub search')
        press(Keys.TAB)
        self.assertEqual(driver.switch_to.active_element.accessible_name, 'Skip to map')
        press(Keys.ENTER)
        self.assertEqual(driver.switch_to.active_element.get_attribute('id'), 'map')

        cluster = driver.find_element(By.CSS_SELECTOR, '#map .marker-cluster-large')
        self.assertRegex(cluster.accessible_name, r'^\d+ pubs, expand group$')
        self.assertEqual(cluster.get_attribute('tabindex'), '0')
        zoom = driver.execute_script('return map.getZoom();')
        cluster.send_keys(Keys.SPACE)
        wait.until(lambda d: d.execute_script('return map.getZoom();') > zoom)
        self.assertEqual(driver.switch_to.active_element.get_attribute('id'), 'map')

        driver.find_element(By.ID, 'skipToSearch').send_keys(Keys.ENTER)
        search = driver.find_element(By.ID, 'searchBox')
        self.assertEqual(driver.switch_to.active_element, search)
        search.send_keys('Bank Tavern')
        count = driver.find_element(By.ID, 'resultCount')
        wait.until(lambda d: count.text == '1 match')
        self.assertEqual(count.aria_role, 'status')
        self.assertEqual(count.get_attribute('aria-atomic'), 'true')
        self.assertEqual(driver.switch_to.active_element, search)

        for element_id in ('nearMeBtn', 'countryFilter', 'areaFilter', 'typeFilter'):
            press(Keys.TAB)
            self.assertEqual(driver.switch_to.active_element.get_attribute('id'), element_id)
        press(Keys.HOME)
        press(Keys.DOWN)
        self.assertEqual(driver.switch_to.active_element.get_attribute('value'), 'Perm')
        for element_id in ('dispenseFilter', 'resetBtn', 'fitBtn'):
            press(Keys.TAB)
            self.assertEqual(driver.switch_to.active_element.get_attribute('id'), element_id)
        press(Keys.TAB)
        result = driver.switch_to.active_element
        self.assertEqual(result.accessible_name, 'View on map, Bank Tavern, Bristol: Central')
        maps_accessible_name = 'Open in Google Maps, Bank Tavern, Bristol: Central'
        self.assertEqual(driver.find_element(By.LINK_TEXT, 'Open in Google Maps').accessible_name,
                         maps_accessible_name)
        press(Keys.ENTER)
        wait.until(EC.visibility_of_element_located((By.ID, 'mobileVenue')))
        self.assertEqual(driver.switch_to.active_element.get_attribute('id'), 'closeMobileVenueBtn')
        press(Keys.ESCAPE)
        pin = wait.until(lambda d: d.switch_to.active_element
                         if d.switch_to.active_element.accessible_name == 'Bank Tavern, Bristol: Central' else False)
        self.assertEqual(pin.accessible_name, 'Bank Tavern, Bristol: Central')
        self.assertFalse(driver.find_elements(By.CSS_SELECTOR, '.leaflet-popup'))
        press(Keys.ENTER)
        heading = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.leaflet-popup h3')))
        self.assertEqual(driver.switch_to.active_element, heading)
        self.assertEqual(heading.text, 'Bank Tavern')
        wait.until(lambda d: d.execute_script("""
            const heading = arguments[0];
            const r = heading.getBoundingClientRect();
            return getComputedStyle(heading.closest('.leaflet-popup')).opacity === '1'
                && getComputedStyle(heading).outlineStyle === 'solid'
                && heading.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
        """, heading))
        self.save_review_screenshot('keyboard-venue-focus.png')
        press(Keys.TAB)
        self.assertEqual(driver.switch_to.active_element.accessible_name, maps_accessible_name)
        press(Keys.ESCAPE)
        self.assertEqual(driver.switch_to.active_element, pin)

    def test_expired_evidence_falls_back_to_approximate_pin(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        payload = json.loads((self.data_root / 'directory-release.json').read_text(encoding='utf-8'))
        row = next(r for r in payload['rows'] if r['pub_name'] == 'Bank Tavern')
        payload['locations'][row['venue_id']]['checked_at'] = '2000-01-01'
        type(self).release_override = payload
        self.load_directory()
        precision = self.driver.execute_script("return pubData.find(p => p.pub_name === 'Bank Tavern').locationPrecision")
        self.assertEqual(precision, 'postcode')
        self.driver.find_element(By.ID, 'searchBox').send_keys('Bank Tavern')
        wait = WebDriverWait(self.driver, 10)
        wait.until(lambda d: d.find_element(By.ID, 'resultCount').text == '1 match')
        pin = self.driver.find_element(By.CSS_SELECTOR, '#map .approximate-marker')
        self.assertIn('Bank Tavern', pin.accessible_name)
        self.assertTrue(pin.accessible_name.endswith('approximate postcode location'))
        card = self.driver.find_element(By.CSS_SELECTOR, '#results .card')
        warning = 'The pin shows the postcode area. Confirm the pub’s exact location before travelling.'
        self.assertIn(warning, card.text)
        link = card.find_element(By.LINK_TEXT, 'Find in Google Maps')
        self.assertIn('Bank Tavern', link.accessible_name)
        self.assertEqual(card.find_elements(By.TAG_NAME, 'a'), [link])
        url = link.get_attribute('href')
        parts = urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ('https', 'www.google.com', '/maps/search/'))
        self.assertEqual(parse_qs(parts.query), {'api': ['1'], 'query': ['Bank Tavern, Bristol: Central, BS1 2HR']})
        self.save_review_screenshot('approximate-location-handoff.png')

        self.open_result_pin_details()
        popup_link = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '#mobileVenue a[target="_blank"]')))
        self.assertEqual(popup_link.text, 'Find in Google Maps')
        self.assertEqual(popup_link.get_attribute('href'), url)
        self.assertIn(warning, self.driver.find_element(By.ID, 'mobileVenueContent').text)

    def test_corrupt_release_does_not_render_a_wrong_pin(self):
        payload = json.loads((self.data_root / 'directory-release.json').read_text(encoding='utf-8'))
        payload['locations'][payload['rows'][0]['venue_id']]['lat'] = 99
        type(self).release_override = payload
        self.load_directory()
        self.assertEqual(self.driver.execute_script('return pubData.length'), 0)
