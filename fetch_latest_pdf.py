"""Download the newest authoritative National Bass Directory edition."""
import argparse
import html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote, urldefrag, urlunsplit
from urllib.request import Request, urlopen


HOME_URL = 'https://nationalbassdirectory.wordpress.com/'
USER_AGENT = 'BassBeerMapBot/1.0'
TIMEOUT_SECONDS = 30
MONTHS = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
          'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
EDITION_RE = re.compile(
    r'^National\s+Bass\s+Directory\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(20\d{2})(?:\s+(?:v(?:ersion)?\s*\d[\w.-]*|revision(?:\s+\S+)?))?$', re.I)
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

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() in ('h1', 'h2', 'h3'):
            self.heading_depth += 1
            self._heading_start = len(self.headings)
            self.headings.append({'text':'','hrefs':[]})
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

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor['text'] += data
        if self.heading_depth and self.headings:
            self.headings[-1]['text'] += data


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
    match = EDITION_RE.fullmatch(' '.join(html.unescape(title).split()))
    if not match:
        return None
    return (int(match.group(2)), MONTHS[match.group(1)[:3].lower()], match.group(1).title(), match.group(2))


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
                candidates.append((key, title, urljoin(HOME_URL, href)))
    if not candidates:
        raise RuntimeError('Could not identify a National Bass Directory month/year edition on the homepage.')
    newest = max(item[0][:2] for item in candidates)
    selected = {(title, url) for key, title, url in candidates if key[:2] == newest}
    if len(selected) != 1:
        raise RuntimeError('The homepage has multiple different posts for its newest directory edition.')
    title, url = next(iter(selected))
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
        normalized = urlunsplit((*urlparse(urldefrag(url)[0])[:4], ''))
        pdf_links.append((normalized, filename, ' '.join(link['text'].split()), link['title']))
    # WordPress renders one attachment link twice (embedded file plus button).
    # Deduplicate by the resolved URL before deciding whether editions conflict.
    unique = {}
    for item in pdf_links:
        unique.setdefault(item[0], item)
    pdf_links = list(unique.values())
    official = [item for item in pdf_links if OFFICIAL_PDF_RE.search(item[1])]
    if len(official) > 1:
        raise RuntimeError('The selected edition post has multiple plausible directory PDFs.')
    if not official:
        titled = [item for item in pdf_links if re.search(r'directory', item[2] + ' ' + item[3], re.I)]
        if len(titled) == 1:
            official = titled
    if len(official) != 1:
        raise RuntimeError('Could not identify exactly one authoritative directory PDF link in the edition post.')
    url, filename, _, _ = official[0]
    expected = edition_key(edition_title) if edition_title else None
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
