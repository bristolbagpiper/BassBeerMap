import json
import unittest
from pathlib import Path
from urllib.request import Request

from request_pages_release import PagesReleaseError, publish_and_verify


COMMIT = 'a' * 40
ASSETS = {
    'index.html': (b'<form id="problemReportForm"></form> directory-release.json latest-bass-directory.pdf '
                   b'https://bassbeermap.bristolbagpiper.workers.dev/api/report-problem'),
    'directory-release.json': b'{"release_id":"current"}',
    'latest-bass-directory.pdf': b'%PDF-test-source',
    'pubs.csv': b'pub_name,postcode\nExample Inn,AB1 1AA\n',
}


class Response:
    def __init__(self, value, status=200):
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class PagesReleaseTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.posts = 0
        self.latest_calls = 0

    def fake_opener(self, request, timeout=20):
        self.assertIsInstance(request, Request)
        self.calls.append((request.get_method(), request.full_url))
        url = request.full_url
        if url.endswith('/pages'):
            return Response({'build_type': 'legacy', 'source': {'branch': 'main', 'path': '/'},
                             'cname': 'bassbeermap.com'})
        if url.endswith('/commits/main'):
            return Response({'sha': COMMIT})
        if url.endswith('/pages/builds/latest'):
            self.latest_calls += 1
            if not self.posts:
                return Response({'commit': 'z' * 40, 'status': 'built'})
            if self.posts and self.latest_calls <= 2:
                return Response({'commit': COMMIT, 'status': 'queued'})
            return Response({'commit': COMMIT, 'status': 'built'})
        if url.endswith('/pages/builds'):
            self.posts += 1
            return Response({'status': 'queued'}, status=201)
        if url.startswith('https://bassbeermap.com/'):
            name = 'index.html' if url.endswith('/') else url.rsplit('/', 1)[-1]
            return Response(ASSETS[name])
        if url == 'https://bassbeermap.bristolbagpiper.workers.dev/api/report-problem?config=1':
            return Response({'turnstileSiteKey': 'fake-public-key'})
        raise AssertionError(f'unexpected request: {request.method} {url}')

    def fake_run(self, args, **kwargs):
        class Result:
            stdout = (COMMIT.encode() if args == ['git', 'rev-parse', 'HEAD']
                      else ASSETS[args[2].split(':', 1)[1]])
        return Result()

    def test_prior_failed_build_can_be_retried_and_feedback_outage_does_not_undo_publication(self):
        def responses(request, timeout=20):
            if request.full_url.endswith('/pages/builds/latest') and not self.posts:
                return Response({'commit': COMMIT, 'status': 'errored', 'updated_at': 'before'})
            if '?config=1' in request.full_url:
                return Response({}, status=503)
            return self.fake_opener(request, timeout)
        result = publish_and_verify(repository='example/map', expected_commit=COMMIT, token='test',
                                    opener=responses, run=self.fake_run, sleep=lambda _: None)
        self.assertEqual(self.posts, 1)
        self.assertEqual(result['build_status'], 'built')
        self.assertFalse(result['feedback_endpoint_configured'])
        self.assertIn('pubs.csv', result['asset_sha256'])

    def test_requests_once_then_matches_build_assets_and_visitor_path(self):
        sleeps = []
        result = publish_and_verify(repository='bristolbagpiper/BassBeerMap', expected_commit=COMMIT,
                                    token='test-token', opener=self.fake_opener,
                                    run=self.fake_run, sleep=sleeps.append,
                                    timeout_seconds=30, poll_seconds=2, monotonic=lambda: 0)
        self.assertEqual(result['commit'], COMMIT)
        self.assertTrue(result['build_requested'])
        self.assertEqual(self.posts, 1)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(any(url.endswith('/latest-bass-directory.pdf') for _, url in self.calls))
        self.assertTrue(any(url.endswith('/directory-release.json') for _, url in self.calls))
        self.assertTrue(any(url == 'https://bassbeermap.com/' for _, url in self.calls))
        self.assertTrue(result['feedback_endpoint_configured'])
        self.assertTrue(any('workers.dev/api/report-problem?config=1' in url for _, url in self.calls))

    def test_matching_completed_build_is_read_back_without_duplicate_request(self):
        original = self.fake_opener

        def already_built(request, timeout=20):
            if request.full_url.endswith('/pages/builds/latest'):
                self.latest_calls += 1
                return Response({'commit': COMMIT, 'status': 'built'})
            return original(request, timeout)

        result = publish_and_verify(repository='bristolbagpiper/BassBeerMap', expected_commit=COMMIT,
                                    token='test-token', opener=already_built, run=self.fake_run,
                                    sleep=lambda _seconds: None, timeout_seconds=10, monotonic=lambda: 0)
        self.assertFalse(result['build_requested'])
        self.assertEqual(self.posts, 0)

    def test_matching_queued_build_is_waited_on_without_duplicate_request(self):
        latest_calls = 0

        def already_queued(request, timeout=20):
            nonlocal latest_calls
            if request.full_url.endswith('/pages/builds/latest'):
                latest_calls += 1
                status = 'queued' if latest_calls == 1 else 'built'
                return Response({'commit': COMMIT, 'status': status})
            return self.fake_opener(request, timeout)

        result = publish_and_verify(repository='bristolbagpiper/BassBeerMap', expected_commit=COMMIT,
                                    token='test-token', opener=already_queued, run=self.fake_run,
                                    sleep=lambda _seconds: None, timeout_seconds=10, monotonic=lambda: 0)
        self.assertFalse(result['build_requested'])
        self.assertEqual(self.posts, 0)

    def test_configuration_or_main_drift_fails_before_build_request(self):
        def drifted(request, timeout=20):
            if request.full_url.endswith('/pages'):
                return Response({'build_type': 'workflow', 'source': {}, 'cname': 'bassbeermap.com'})
            return self.fake_opener(request, timeout)

        with self.assertRaisesRegex(PagesReleaseError, 'configuration changed'):
            publish_and_verify(repository='bristolbagpiper/BassBeerMap', expected_commit=COMMIT,
                                token='test-token', opener=drifted, run=self.fake_run)
        self.assertEqual(self.posts, 0)

        def advanced(request, timeout=20):
            if request.full_url.endswith('/commits/main'):
                return Response({'sha': 'b' * 40})
            return self.fake_opener(request, timeout)

        with self.assertRaisesRegex(PagesReleaseError, 'main advanced'):
            publish_and_verify(repository='bristolbagpiper/BassBeerMap', expected_commit=COMMIT,
                                token='test-token', opener=advanced, run=self.fake_run)
        self.assertEqual(self.posts, 0)

    def test_only_changed_import_and_approved_correction_call_shared_handoff(self):
        workflows = Path(__file__).resolve().parents[1] / '.github' / 'workflows'
        for name, publication in (('update-csv-from-pdf.yml', 'steps.publication.outputs.state == \'published\''),
                                  ('approve-location.yml', 'steps.publication.outputs.state == \'published\'')):
            with self.subTest(workflow=name):
                source = (workflows / name).read_text(encoding='utf-8')
                self.assertIn('pages: write', source)
                self.assertIn('id: pages_release', source)
                self.assertIn(publication, source)
                self.assertIn('run: python request_pages_release.py', source)
                self.assertIn('EXPECTED_PAGES_COMMIT: ${{ steps.publication.outputs.commit }}', source)
                self.assertIn('BassBeerMap Pages deployment failed', source)
        self.assertNotIn('request_pages_release.py', (workflows / 'weekly-map-validation.yml').read_text(encoding='utf-8'))

    def test_unchanged_schedule_compares_source_before_expensive_steps_and_short_circuits_only_it(self):
        source = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'update-csv-from-pdf.yml').read_text(encoding='utf-8')
        fingerprint = source.index('id: source_fingerprint')
        self.assertLess(source.index('name: Download latest PDF from WordPress'), fingerprint)
        self.assertLess(source.index('id: source_fingerprint'), source.index('name: Install dependencies'))
        self.assertIn("if: github.event_name == 'schedule' && steps.pdf_source.outputs.mode == 'wordpress'", source)
        self.assertIn("if: steps.source_fingerprint.outputs.unchanged != 'true'", source)
        for step in ('Install dependencies', 'Run data pipeline tests', 'Run browser smoke test on the current release',
                     'Generate candidate CSV', 'Reconcile, enrich and validate the complete candidate release',
                     'Run browser smoke test against the candidate release', 'Publish the complete validated release'):
            block = source.split(f'name: {step}', 1)[1].split('\n      - name:', 1)[0]
            self.assertIn("steps.source_fingerprint.outputs.unchanged != 'true'", block, step)

if __name__ == '__main__':
    unittest.main()
