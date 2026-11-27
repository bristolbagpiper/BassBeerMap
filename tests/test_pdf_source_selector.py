import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from fetch_latest_pdf import download_latest_pdf, find_latest_post, find_pdf_url
from guard_unchanged_pdf import main as guard_unchanged_main
from pdf_source_selector import select


class PdfSourceSelectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        self.runner = Path(self.temp.name) / 'runner'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def event(before='base', after='head'):
        return {'before': before, 'after': after}

    def git(self, files, blob=b'%PDF-upload-content'):
        def run(args, **kwargs):
            if args[1] == 'diff':
                return b''.join(status.encode() + b'\0' + path.encode() + b'\0' for status, path in files)
            if args[1] == 'show':
                return blob
            raise AssertionError(args)
        return run

    def test_code_only_push_skips_import(self):
        selected = select('push', self.event(), workspace=self.root, runner_temp=self.runner,
                          run=self.git([('M', 'ingest_directory.py')]))
        self.assertEqual(selected['mode'], 'skip')

    def test_single_noncanonical_upload_extracts_exact_commit_blob_without_touching_repo_pdfs(self):
        unrelated = self.root / 'archive.pdf'
        unrelated.write_bytes(b'%PDF-unrelated')
        selected = select('push', self.event(), workspace=self.root, runner_temp=self.runner,
                          run=self.git([('A', 'Bass Directory November 2026.pdf')]))
        self.assertEqual(selected['mode'], 'upload')
        self.assertEqual(Path(selected['source_pdf']).read_bytes(), b'%PDF-upload-content')
        self.assertEqual(unrelated.read_bytes(), b'%PDF-unrelated')
        self.assertEqual(Path(selected['source_name_file']).read_text(encoding='utf-8'), '"Bass Directory November 2026.pdf"')

    def test_canonical_pdf_change_is_explicit_full_release_validation_mode(self):
        selected = select('push', self.event(), workspace=self.root, runner_temp=self.runner,
                          run=self.git([('M', 'latest-bass-directory.pdf')]))
        self.assertEqual(selected['mode'], 'canonical_release')
        self.assertEqual(Path(selected['source_pdf']), self.root / 'latest-bass-directory.pdf')

    def test_multiple_changed_pdfs_are_rejected_without_selecting_one_alphabetically(self):
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous PDF submission'):
            select('push', self.event(), workspace=self.root, runner_temp=self.runner,
                   run=self.git([('A', 'A.pdf'), ('M', 'Z.pdf')]))

    def test_nested_upload_and_non_pdf_blob_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'repository root'):
            select('push', self.event(), workspace=self.root, runner_temp=self.runner,
                   run=self.git([('A', 'uploads/new.pdf')]))
        with self.assertRaisesRegex(RuntimeError, 'not a PDF'):
            select('push', self.event(), workspace=self.root, runner_temp=self.runner,
                   run=self.git([('A', 'new.pdf')], blob=b'not a pdf'))

    def test_deleted_noncanonical_pdf_skips_and_workflow_manual_run_uses_wordpress(self):
        removed = select('push', self.event(), workspace=self.root, runner_temp=self.runner,
                         run=self.git([('D', 'old-source.pdf')]))
        self.assertEqual(removed['mode'], 'skip')
        manual = select('workflow_dispatch', {}, workspace=self.root, runner_temp=self.runner)
        self.assertEqual(manual['mode'], 'wordpress')

    def test_workflow_uses_only_pdf_pushes_and_validates_canonical_release_in_ci(self):
        repo = Path(__file__).resolve().parents[1]
        importer = (repo / '.github' / 'workflows' / 'update-csv-from-pdf.yml').read_text(encoding='utf-8')
        self.assertIn('paths: ["*.pdf"]', importer)
        self.assertIn('run: python pdf_source_selector.py', importer)
        self.assertIn('fetch-depth: 0', importer)
        self.assertNotIn('rm -f', importer)
        self.assertNotIn('mv "$uploaded_pdf"', importer)
        self.assertIn("'mode': 'canonical_release'", Path(__file__).resolve().parents[1].joinpath('pdf_source_selector.py').read_text(encoding='utf-8'))
        ci = (repo / '.github' / 'workflows' / 'validate-coordinates.yml').read_text(encoding='utf-8')
        self.assertIn('pip install -r requirements.txt', ci)
        self.assertRegex((repo / 'requirements.txt').read_text(encoding='utf-8'),
                         r'(?m)^pypdf==[0-9]+(?:\.[0-9]+)+$')
        self.assertIn('python validate_directory_release.py --check-pdf', ci)

    def test_unchanged_source_guard_uses_event_baseline_and_selected_upload(self):
        uploaded = self.root / 'upload.pdf'
        published_csv = self.root / 'published.csv'
        candidate_csv = self.root / 'candidate.csv'
        uploaded.write_bytes(b'%PDF-new-source')
        published_csv.write_text('pub_name,venue_id\nOld,v1\n', encoding='utf-8')
        candidate_csv.write_text('pub_name,venue_id\nNew,v2\n', encoding='utf-8')
        with patch('guard_unchanged_pdf.subprocess.run', return_value=CompletedProcess(
                args=[], returncode=0, stdout=b'%PDF-old-source', stderr=b'')) as run:
            guard_unchanged_main(['--source-pdf', str(uploaded), '--baseline-ref', 'push-before',
                                  '--published-csv', str(published_csv), '--candidate-csv', str(candidate_csv)])
        self.assertEqual(run.call_args.args[0], ['git', 'show', 'push-before:latest-bass-directory.pdf'])

    def test_wordpress_page_edition_and_attachment_are_selected_without_first_link_assumption(self):
        home = '''<h2><a href="/2026/10/01/old/">National Bass Directory October 2026</a></h2>
                  <h2><a href="/2026/11/01/national-bass-directory-november-2026/">National Bass Directory November 2026</a></h2>'''
        post_url = 'https://nationalbassdirectory.wordpress.com/2026/11/01/national-bass-directory-november-2026/'
        post = '''<a href="https://nationalbassdirectory.wordpress.com/wp-content/uploads/2026/11/terms.pdf">Terms</a>
                  <a href="https://nationalbassdirectory.wordpress.com/wp-content/uploads/2026/11/bass-master-directory-november-2026.pdf">Edition</a>'''
        title, selected_post = find_latest_post(home)
        self.assertEqual(title, 'National Bass Directory November 2026')
        self.assertEqual(selected_post, post_url)
        self.assertEqual(find_pdf_url(post, post_url, title),
                         'https://nationalbassdirectory.wordpress.com/wp-content/uploads/2026/11/bass-master-directory-november-2026.pdf')

    def test_wordpress_duplicate_same_attachment_and_revision_suffix_are_supported(self):
        post_url = 'https://example.test/2026/11/post/'
        title = 'National Bass Directory November 2026'
        attachment = 'https://example.test/files/bass-master-directory-november-2026-v2-with-pcodes.pdf'
        repeated = f'<a href="{attachment}">Embedded PDF</a><a href="{attachment}">Download</a>'
        self.assertEqual(find_pdf_url(repeated, post_url, title), attachment)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'latest-bass-directory.pdf'
            with patch('fetch_latest_pdf.fetch_text', side_effect=[
                    f'<h2><a href="{post_url}">{title}</a></h2>', repeated]), \
                    patch('fetch_latest_pdf.fetch_bytes', return_value=b'%PDF-test'):
                download_latest_pdf(destination)
            self.assertEqual(destination.read_bytes(), b'%PDF-test')

    def test_wordpress_distinct_directory_pdfs_or_mismatched_edition_fail_closed(self):
        title = 'National Bass Directory November 2026'
        post_url = 'https://example.test/post/'
        first = '<a href="/bass-master-directory-november-2026.pdf">One</a>'
        second = '<a href="/national-bass-directory-november-2026.pdf">Two</a>'
        with self.assertRaisesRegex(RuntimeError, 'multiple plausible'):
            find_pdf_url(first + second, post_url, title)
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            find_pdf_url('<a href="/bass-master-directory-october-2026.pdf">Old</a>', post_url, title)


if __name__ == '__main__':
    unittest.main()
