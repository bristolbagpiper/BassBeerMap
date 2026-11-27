"""Download the newest authoritative National Bass Directory edition."""
import argparse
from datetime import date
import html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote, urldefrag
from urllib.request import Request, urlopen


HOME_URL = 'https://nationalbassdirectory.wordpress.com/'
USER_AGENT = 'BassBeerMapBot/1.0'
TIMEOUT_SECONDS = 30
MONTHS = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
          'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
EDITION_RE = re.compile(
    r'^National\s+Bass\s+Directory\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(20\d{2})(?:\s*(?:[-–—]\s*)?(?:(?:v|version|revision|rev)(?:\s*[\w.-]+)?|(?:revised|updated|update|corrected|correction)(?:\s+[\w.-]+)?))?$', re.I)
EDITION_TOKEN_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|October|November|December)[-_\s]+(20\d{2})', re.I)
OFFICIAL_PDF_RE = re.compile(r'(?:national[-_ ]bass[-_ ]directory|bass[-_ ]master[-_ ]directory|bass[-_ ]directory)', re.I)


class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading_depth = 0
        self.anchor = None
        self.headings = []
        self.links = []
        self.post = None
        self.post_list_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == 'li':
            classes = set((attrs.get('class') or '').split())
            if self.post is None and 'wp-block-post' in classes:
                self.post = {'text': '', 'dates': []}
                self.post_list_depth = 1
            elif self.post is not None:
                self.post_list_depth += 1
        if tag.lower() == 'time' and self.post is not None and attrs.get('datetime'):
            self.post['dates'].append(attrs['datetime'])
        if tag.lower() in ('h1', 'h2', 'h3'):
            self.heading_depth += 1
            self._heading_start = len(self.headings)
            self.headings.append({'text':'','hrefs':[],'post':self.post})
        if tag.lower() == 'a':
            self.anchor = {'href':attrs.get('href',''), 'text':'', 'title':attrs.get('title','')}
            self.links.append(self.anchor)
            if self.heading_depth and self.headings:
                self.headings[-1]['hrefs'].append(self.anchor['href'])

    def handle_endtag(self, tag):
        if tag.lower() == 'a':
            self.anchor = None
        if tag.lower() in ('h1', 'h2', 'h3') and self.heading_depth:
            self.heading_depth -= 1
        if tag.lower() == 'li' and self.post is not None:
            self.post_list_depth -= 1
            if self.post_list_depth == 0:
                self.post = None

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor['text'] += data
        if self.heading_depth and self.headings:
            self.headings[-1]['text'] += data
        if self.post is not None:
            self.post['text'] += data


def fetch_text(url: str) -> str:
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode('utf-8', errors='replace')


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get_content_type()
        payload = response.read()
    if content_type != 'application/pdf':
        raise RuntimeError(f"Expected a PDF download but received '{content_type}'.")
    if not payload.startswith(b'%PDF-'):
        raise RuntimeError('Downloaded file does not have a valid PDF signature.')
    return payload


def edition_key(title):
    match = EDITION_RE.fullmatch(edition_heading(title))
    if not match:
        return None
    return (int(match.group(2)), MONTHS[match.group(1)[:3].lower()], match.group(1).title(), match.group(2))


def edition_heading(title):
    title = ' '.join(html.unescape(title).split())
    return re.sub(r'\s*\(([^()]*)\)$', r' \1', title)


def edition_revision(title):
    """Return an explicit numeric in-month revision, or None if unnumbered."""
    title = edition_heading(title)
    match = EDITION_RE.fullmatch(title)
    if not match:
        return None
    suffix = title[match.end(2):].strip()
    if not suffix:
        return None
    return revision_number(suffix)


def revision_number(text):
    match = re.search(r'(?i)(?:^|[-_\s])(?:version|v|revision|rev|revised)[-_\s]*(\d+(?:\.\d+)*)', text)
    return tuple(map(int, match.group(1).split('.'))) if match else None


def homepage_post_date(post):
    """Extract only an explicit WordPress publish date attached to a post item."""
    if not post:
        return None
    for value in post['dates']:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            continue
    visible = ' '.join(html.unescape(post['text']).split())
    match = re.search(
        r'(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+'
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
        r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+'
        r'(20\d{2})\b', visible)
    if not match:
        return None
    return date(int(match.group(3)), MONTHS[match.group(2)[:3].lower()], int(match.group(1)))


def attachment_revision(item):
    """Read a numeric revision from a PDF name or its visible link label."""
    evidence = ' '.join((item[1], item[2], item[3]))
    return revision_number(evidence)


def find_latest_post(home_html: str):
    parser = AnchorParser()
    parser.feed(home_html)
    candidates = []
    for heading in parser.headings:
        title = ' '.join(html.unescape(heading['text']).split())
        key = edition_key(title)
        hrefs = sorted({html.unescape(href) for href in heading['hrefs'] if href})
        if key and hrefs:
            for href in hrefs:
                candidates.append((key, edition_revision(title), homepage_post_date(heading['post']),
                                   title, urljoin(HOME_URL, href)))
    if not candidates:
        raise RuntimeError('Could not identify a National Bass Directory month/year edition on the homepage.')
    newest = max(item[0][:2] for item in candidates)
    selected = [item for item in candidates if item[0][:2] == newest]
    # Duplicate links to the same post are harmless. Distinct posts for the
    # current month need an explicit date or numeric revision order; guessing
    # by HTML position or URL could silently select a stale/incorrect PDF.
    by_url = {}
    for key, revision, published, title, url in selected:
        by_url.setdefault(url, (key, revision, published, title, url))
    selected = list(by_url.values())
    if len(selected) > 1:
        # WordPress's authoritative post list carries publication dates; use
        # them before slugs, which may receive an arbitrary "-1" collision suffix.
        if all(item[2] is not None for item in selected):
            newest_date = max(item[2] for item in selected)
            latest = [item for item in selected if item[2] == newest_date]
            selected = latest
        if len(selected) > 1:
            if not any(item[1] is not None for item in selected):
                raise RuntimeError('The homepage has multiple posts for its newest directory month without a unique published date or explicit revision number.')
            highest_revision = max(item[1] or (0,) for item in selected)
            latest = [item for item in selected
                      if (item[1] or (0,)) == highest_revision]
            if len(latest) != 1:
                raise RuntimeError('The homepage has multiple different posts for its newest directory revision/date.')
            selected = latest
    _, _, _, title, url = selected[0]
    return title, url


def find_latest_post_url(home_html: str) -> str:
    """Compatibility wrapper returning only the selected edition URL."""
    return find_latest_post(home_html)[1]


def find_pdf_url(post_html: str, post_url: str, edition_title: str = '') -> str:
    parser = AnchorParser()
    parser.feed(post_html)
    pdf_links = []
    for link in parser.links:
        href = html.unescape(link['href'].strip())
        if not href:
            continue
        url = urljoin(post_url, href)
        path = unquote(urlparse(url).path)
        filename = path.rsplit('/', 1)[-1]
        if not filename.lower().endswith('.pdf'):
            continue
        # Keep query strings: a revision may be exposed at the same path with
        # a distinct query-based URL. Fragments are client-side only.
        normalized = urldefrag(url)[0]
        pdf_links.append((normalized, filename, ' '.join(link['text'].split()), link['title']))
    # WordPress renders one attachment link twice (embedded file plus button).
    # Deduplicate by the resolved URL before deciding whether editions conflict.
    unique = {}
    for item in pdf_links:
        if item[0] in unique:
            url, filename, label, title = unique[item[0]]
            unique[url] = (url, filename, label + ' ' + item[2], title + ' ' + item[3])
        else:
            unique[item[0]] = item
    pdf_links = list(unique.values())
    official = [item for item in pdf_links if OFFICIAL_PDF_RE.search(item[1])]
    expected = edition_key(edition_title) if edition_title else None
    if expected:
        matching_or_unlabelled = []
        for item in official:
            filename_edition = EDITION_TOKEN_RE.search(item[1])
            if not filename_edition:
                matching_or_unlabelled.append(item)
                continue
            actual = (int(filename_edition.group(2)), MONTHS[filename_edition.group(1)[:3].lower()])
            if actual == expected[:2]:
                matching_or_unlabelled.append(item)
        if matching_or_unlabelled:
            official = matching_or_unlabelled
    if len(official) > 1:
        series = {re.sub(r'[-_ ]+', ' ', OFFICIAL_PDF_RE.search(item[1]).group().lower()) for item in official}
        if len(series) != 1:
            raise RuntimeError('The edition post links multiple plausible directory PDF series.')
        revisions = [(attachment_revision(item), item) for item in official]
        plain = [item for revision, item in revisions if revision is None]
        if (len(plain) > 1 or (plain and (not re.search(r'20\d{2}\.pdf$', plain[0][1], re.I)
                or re.search(r'revis|updat|correct', ' '.join(plain[0][2:]), re.I)))):
            raise RuntimeError('The selected edition post has multiple plausible directory PDFs without explicit revision numbers.')
        highest_revision = max(revision for revision, _ in revisions if revision is not None)
        latest = [item for revision, item in revisions if revision == highest_revision]
        if len(latest) != 1:
            raise RuntimeError('The selected edition post has multiple plausible PDFs at the same revision.')
        official = latest
    if not official:
        titled = [item for item in pdf_links if re.search(r'directory', item[2] + ' ' + item[3], re.I)]
        if len(titled) == 1:
            official = titled
    if len(official) != 1:
        raise RuntimeError('Could not identify exactly one authoritative directory PDF link in the edition post.')
    url, filename, _, _ = official[0]
    filename_edition = EDITION_TOKEN_RE.search(filename)
    if expected and filename_edition:
        actual = (int(filename_edition.group(2)), MONTHS[filename_edition.group(1)[:3].lower()])
        if actual != expected[:2]:
            raise RuntimeError('The directory PDF filename does not match the selected post edition.')
    return url


def download_latest_pdf(output_path: Path) -> None:
    home_html = fetch_text(HOME_URL)
    edition_title, post_url = find_latest_post(home_html)
    post_html = fetch_text(post_url)
    pdf_url = find_pdf_url(post_html, post_url, edition_title)
    pdf_bytes = fetch_bytes(pdf_url)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f'.{output_path.name}.download')
    try:
        temporary_path.write_bytes(pdf_bytes)
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f'Downloaded {edition_title} from {pdf_url} to {output_path}')


def main() -> None:
    parser = argparse.ArgumentParser(description='Download the newest National Bass Directory PDF from WordPress.')
    parser.add_argument('output', nargs='?', default='latest-bass-directory.pdf', help='Path to write the downloaded PDF to.')
    args = parser.parse_args()
    download_latest_pdf(Path(args.output))


if __name__ == '__main__':
    main()
