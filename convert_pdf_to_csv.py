import argparse
import csv
import json
import re
from pathlib import Path

from pypdf import PdfReader


COUNTRIES = {
    "ENGLAND": "England",
    "WALES": "Wales",
    "SCOTLAND": "Scotland",
    "NORTHERN IRELAND": "Northern Ireland",
    "CHANNEL ISLANDS": "Channel Islands",
    "ISLE OF MAN": "Isle of Man",
}

STATUS_VALUES = {"Perm", "Guest"}
POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$|^[A-Z]{2}\d ?\d[A-Z]{2}$")
LAST_RE = re.compile(r"^20\d{2} Q[1-4]$")
DISPENSE_RE = re.compile(r"^[HGEJBP](?:/[HGEJBP])*$")

CSV_COLUMNS = [
    "country",
    "area",
    "pub_name",
    "place_name",
    "postcode",
    "pg",
    "last",
    "dispense",
    "notes",
]

MONTH_NAMES = {
    "jan": "January",
    "feb": "February",
    "mar": "March",
    "apr": "April",
    "may": "May",
    "jun": "June",
    "jul": "July",
    "aug": "August",
    "sep": "September",
    "oct": "October",
    "nov": "November",
    "dec": "December",
}

MONTH_YEAR_RE = re.compile(
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[-_ ]+(?P<year>20\d{2})",
    re.IGNORECASE,
)
YEAR_MONTH_RE = re.compile(
    r"(?P<year>20\d{2})[-_ ]+(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)",
    re.IGNORECASE,
)
CREATION_DATE_RE = re.compile(r"D:(?P<year>\d{4})(?P<month>\d{2})")

SKIP_PREFIXES = (
    "UK Area",
    "Pub Name",
    "Place Name",
    "Pcode",
    "P/G",
    "Last",
    "Ds",
    "Source/Notes",
    "BASS DIRECTORY",
    "This is a list",
    "P/G column",
    "This directory",
    "The directory",
    "Key:",
    "If Bass is reported",
    "v2.0",
    "'once ayear'",
)

STOP_PREFIXES = (
    "Removed pubs",
    "Removal month",
    "Coverage & Notes",
    "Contact details",
    "Total 1088",
)

SKIP_CONTAINS = ("facebook.com/groups/nationalbassday",)
SKIP_EXACT = {"not"}


def parse_pdf_rows(reader: PdfReader) -> list[dict[str, str]]:
    items: list[dict[str, float | str]] = []

    for page_number, page in enumerate(reader.pages, start=1):
      def visitor(text, cm, tm, font_dict, font_size):
          if text.strip():
              items.append(
                  {
                      "page": page_number,
                      "text": text,
                      "x": tm[4],
                      "y": tm[5],
                  }
              )

      page.extract_text(visitor_text=visitor)

    rows: list[dict[str, str]] = []
    current_country = ""
    current_area = ""
    current_row: dict[str, str] | None = None
    started = False

    def finish_row() -> None:
        nonlocal current_row
        if current_row and current_row["pub_name"] and current_row["postcode"] and current_row["pg"]:
            if not current_row["dispense"]:
                current_row["dispense"] = "H"
            rows.append(current_row)
        current_row = None

    for item in items:
        raw = str(item["text"])
        text = raw.strip()
        x = float(item["x"])
        y = float(item["y"])
        positioned = abs(y) > 0.01

        if not text or text in SKIP_EXACT:
            continue
        if any(text.startswith(prefix) for prefix in STOP_PREFIXES):
            finish_row()
            break
        if any(text.startswith(prefix) for prefix in SKIP_PREFIXES):
            continue
        if any(fragment in text for fragment in SKIP_CONTAINS):
            continue

        if text in COUNTRIES:
            finish_row()
            current_country = COUNTRIES[text]
            if current_country in {"Scotland", "Northern Ireland", "Channel Islands", "Isle of Man"}:
                current_area = ""
            started = True
            continue

        if not started:
            continue

        if positioned and x < 100:
            finish_row()
            current_area = text.title() if text.isupper() else text
            continue

        normalised_text = text.upper() if POSTCODE_RE.match(text.upper()) else text
        is_postcode = bool(POSTCODE_RE.match(normalised_text))
        is_status = text in STATUS_VALUES
        is_last = bool(LAST_RE.match(text))
        is_dispense = bool(DISPENSE_RE.match(text))
        is_note_column = x >= 720

        if current_row is None:
            if is_postcode or is_status or is_last or is_dispense:
                continue
            current_row = {
                "country": current_country,
                "area": current_area,
                "pub_name": text,
                "place_name": "",
                "postcode": "",
                "pg": "",
                "last": "",
                "dispense": "",
                "notes": "",
            }
            continue

        if is_postcode:
            current_row["postcode"] = normalised_text
            continue
        if is_status:
            current_row["pg"] = text
            continue
        if is_last:
            current_row["last"] = text
            continue
        if is_dispense:
            current_row["dispense"] = text
            continue
        if is_note_column:
            current_row["notes"] = (current_row["notes"] + " " + text).strip()
            continue
        if not current_row["place_name"]:
            current_row["place_name"] = text
            continue
        if current_row["pg"] and current_row["last"] and positioned:
            finish_row()
            current_row = {
                "country": current_country,
                "area": current_area,
                "pub_name": text,
                "place_name": "",
                "postcode": "",
                "pg": "",
                "last": "",
                "dispense": "",
                "notes": "",
            }
            continue

        current_row["notes"] = (current_row["notes"] + " " + text).strip()

    finish_row()
    return rows


def write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def apply_postcode_overrides(rows: list[dict[str, str]], overrides_path: Path) -> None:
    """Apply documented source corrections before publishing the CSV."""
    if not overrides_path.exists():
        return
    overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
    for row in rows:
        override = overrides.get(row["postcode"])
        if override and override.get("postcode"):
            row["postcode"] = str(override["postcode"]).upper()


def normalise_month_name(raw: str) -> str | None:
    key = raw.strip().lower()[:3]
    return MONTH_NAMES.get(key)


def extract_month_year(value: str) -> str | None:
    for pattern in (MONTH_YEAR_RE, YEAR_MONTH_RE):
        match = pattern.search(value)
        if match:
            month_name = normalise_month_name(match.group("month"))
            year = match.group("year")
            if month_name:
                return f"{month_name} {year}"
    return None


def month_year_from_creation_date(value: str) -> str | None:
    match = CREATION_DATE_RE.search(value)
    if not match:
        return None

    month_number = int(match.group("month"))
    if not 1 <= month_number <= 12:
        return None

    month_name = list(MONTH_NAMES.values())[month_number - 1]
    return f"{month_name} {match.group('year')}"


def extract_directory_metadata(pdf_path: Path, reader: PdfReader, source_name: str | None = None) -> dict[str, str]:
    month_year = None
    for candidate in (source_name or "", pdf_path.name, pdf_path.stem):
        if not candidate:
            continue
        month_year = extract_month_year(candidate)
        if month_year:
            break

    if not month_year:
        creation_date = str((reader.metadata or {}).get("/CreationDate", "")).strip()
        month_year = month_year_from_creation_date(creation_date)

    directory_label = f"{month_year} Bass directory" if month_year else "latest Bass directory"
    return {
        "directory_label": directory_label,
        "directory_month_year": month_year or "",
        "source_pdf": pdf_path.name,
        "source_name_hint": source_name or "",
    }


def write_metadata(metadata: dict[str, str], output_path: Path) -> None:
    output_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert the Bass directory PDF into pubs.csv.")
    parser.add_argument(
        "pdf",
        nargs="?",
        default="bass-master-directory-april-2026-v2-with-pcodes.pdf",
        help="Path to the source PDF.",
    )
    parser.add_argument(
        "--postcode-overrides",
        default="postcode-overrides.json",
        help="JSON file containing documented postcode corrections.",
    )
    parser.add_argument(
        "csv",
        nargs="?",
        default="pubs.csv",
        help="Path to the output CSV.",
    )
    parser.add_argument(
        "--metadata",
        default="directory-meta.json",
        help="Path to the output metadata JSON.",
    )
    parser.add_argument(
        "--source-name",
        default="",
        help="Original source PDF name used to derive the directory label.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    csv_path = Path(args.csv)
    metadata_path = Path(args.metadata)

    reader = PdfReader(str(pdf_path))
    rows = parse_pdf_rows(reader)
    apply_postcode_overrides(rows, Path(args.postcode_overrides))
    metadata = extract_directory_metadata(pdf_path, reader, args.source_name or None)
    write_csv(rows, csv_path)
    write_metadata(metadata, metadata_path)
    print(f"Wrote {len(rows):,} rows to {csv_path}")
    print(f"Wrote directory metadata to {metadata_path}")


if __name__ == "__main__":
    main()
