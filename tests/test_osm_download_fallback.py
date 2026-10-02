"""Exercise the actual workflow download shell with a controlled curl result."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else None


@unittest.skipUnless(BASH and Path(BASH).exists(), "Bash is required for workflow regression tests")
class OsmDownloadFallbackTests(unittest.TestCase):
    def run_download(self, status):
        workflow = (ROOT / ".github/workflows/update-csv-from-pdf.yml").read_text(encoding="utf-8")
        step = workflow.split("      - name: Download current Great Britain OpenStreetMap extract\n", 1)[1].split("      - name:", 1)[0]
        script = re.sub(r"^          ", "", step.split("        run: |\n", 1)[1], flags=re.MULTILINE)
        fake_curl = f'curl() {{ printf "download bytes" > great-britain-latest.osm.pbf; return {status}; }}\n'
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            existing = '{"venues":{"existing pub":{"lat":51.45,"lng":-2.59}}}'
            (work / "venue-coordinates.json").write_text(existing, encoding="utf-8")
            env = dict(os.environ, GITHUB_OUTPUT="step-output.txt", GITHUB_STEP_SUMMARY="summary.txt")
            result = subprocess.run([BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", fake_curl + script], cwd=work, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = (work / "step-output.txt").read_text()
            if status:
                self.assertIn("available=false", output)
                self.assertFalse((work / "great-britain-latest.osm.pbf").exists())
                self.assertEqual((work / "candidate-venue-coordinates.json").read_text(), existing)
                self.assertIn(f"curl exit {status}", result.stdout)
                self.assertIn("manual review queue", (work / "summary.txt").read_text())
            else:
                self.assertIn("available=true", output)
                self.assertTrue((work / "great-britain-latest.osm.pbf").exists())
                self.assertFalse((work / "candidate-venue-coordinates.json").exists())

    def test_redirect_loop_preserves_coordinates_and_allows_publication(self):
        self.run_download(47)

    def test_http_failure_preserves_coordinates_and_allows_publication(self):
        self.run_download(22)

    def test_success_keeps_extract_for_osm_resolution(self):
        self.run_download(0)
