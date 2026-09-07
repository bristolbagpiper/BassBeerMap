import os
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Browser smoke tests run in CI")
class FrontendSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.chdir(Path(__file__).resolve().parents[1])
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
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
        self.assertEqual(self.driver.find_element(By.ID, "resultCount").text, "1,103 matches")
        self.driver.execute_script("setUserLocation(51.5, -0.1, 'Test location');")
        self.assertEqual(self.driver.find_element(By.ID, "typeFilter").get_attribute("value"), "Perm")
