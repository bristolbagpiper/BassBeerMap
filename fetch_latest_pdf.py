import argparse
import html
import re
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen


HOME_URL = "https://nationalbassdirectory.wordpress.com/"
USER_AGENT = "BassBeerMapBot/1.0"
TIMEOUT_SECONDS = 30


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get_content_type()
        payload = response.read()
    if content_type != "application/pdf":
        raise RuntimeError(f"Expected a PDF download but received '{content_type}'.")
    if not payload.startswith(b"%PDF-"):
        raise RuntimeError("Downloaded file does not have a valid PDF signature.")
    return payload


def find_latest_post_url(home_html: str) -> str:
    match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', home_html, re.IGNORECASE)
    if not match:
        raise RuntimeError("Could not find the latest post URL on the Bass directory homepage.")
    return html.unescape(match.group(1))


def find_pdf_url(post_html: str, post_url: str) -> str:
    match = re.search(r'href="([^"]+\.pdf(?:\?[^"]*)?)"', post_html, re.IGNORECASE)
    if not match:
        raise RuntimeError("Could not find a PDF download link on the latest Bass directory post.")
    return urljoin(post_url, html.unescape(match.group(1)))


def download_latest_pdf(output_path: Path) -> None:
    home_html = fetch_text(HOME_URL)
    post_url = find_latest_post_url(home_html)
    post_html = fetch_text(post_url)
    pdf_url = find_pdf_url(post_html, post_url)
    pdf_bytes = fetch_bytes(pdf_url)

    output_path.write_bytes(pdf_bytes)
    print(f"Downloaded latest PDF from {pdf_url} to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download the latest Bass directory PDF from WordPress.")
    parser.add_argument(
        "output",
        nargs="?",
        default="latest-bass-directory.pdf",
        help="Path to write the downloaded PDF to.",
    )
    args = parser.parse_args()

    download_latest_pdf(Path(args.output))


if __name__ == "__main__":
    main()
