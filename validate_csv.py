import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


REQUIRED_FIELDS = ("country", "pub_name", "place_name", "postcode", "pg", "last", "dispense")
MAX_ROW_DELTA_RATIO = 0.08
MAX_BLANK_RATIO = 0.02
MAX_COUNTRY_DELTA = 0.2
# Percentage-only checks are overly sensitive for countries with only a few
# listings: a change of two entries is 22.2% when the previous total is nine.
# Keep the relative guard for material changes, while accepting small absolute
# variations that are expected as the directory is refreshed.
MAX_COUNTRY_ABSOLUTE_DELTA = 2
POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}$")
LAST_RE = re.compile(r"^20\d{2} Q[1-4]$")
DISPENSE_RE = re.compile(r"^[HGEJBP](?:/[HGEJBP])*$")


def quarter_key(value: str) -> tuple[int, int]:
    value = str(value).strip()
    if not value:
        return (0, 0)
    year_text, quarter_text = value.split()
    return (int(year_text), int(quarter_text.replace("Q", "")))


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def blank_ratio(rows: list[dict[str, str]], field: str) -> float:
    if not rows:
        return 1.0
    blanks = sum(1 for row in rows if not str(row.get(field, "")).strip())
    return blanks / len(rows)


def country_counts(rows: list[dict[str, str]]) -> Counter[str]:
    return Counter(str(row.get("country", "")).strip() for row in rows if str(row.get("country", "")).strip())


def validate(previous_rows: list[dict[str, str]], candidate_rows: list[dict[str, str]]) -> tuple[bool, list[str], dict]:
    errors: list[str] = []
    summary = {
        "previous_row_count": len(previous_rows),
        "candidate_row_count": len(candidate_rows),
        "field_blank_ratios": {},
        "country_deltas": {},
    }

    if not candidate_rows:
        errors.append("Candidate CSV has no rows.")
        return False, errors, summary

    for field in REQUIRED_FIELDS:
        ratio = blank_ratio(candidate_rows, field)
        summary["field_blank_ratios"][field] = ratio
        if ratio > MAX_BLANK_RATIO:
            errors.append(f"Field '{field}' is blank in {ratio:.1%} of candidate rows.")

    for index, row in enumerate(candidate_rows, start=2):
        postcode = row.get("postcode", "").strip().upper()
        if not POSTCODE_RE.match(postcode):
            errors.append(f"Row {index} has an invalid postcode: '{postcode}'.")
        if row.get("pg", "").strip() not in {"Perm", "Guest"}:
            errors.append(f"Row {index} has an invalid P/G value.")
        if not LAST_RE.match(row.get("last", "").strip()):
            errors.append(f"Row {index} has an invalid last-verified quarter.")
        if not DISPENSE_RE.match(row.get("dispense", "").strip()):
            errors.append(f"Row {index} has an invalid dispense value.")
        if len(errors) >= 25:
            errors.append("Further row-level validation errors omitted.")
            break

    if previous_rows:
        previous_count = len(previous_rows)
        candidate_count = len(candidate_rows)
        delta_ratio = abs(candidate_count - previous_count) / previous_count
        summary["row_delta_ratio"] = delta_ratio
        if delta_ratio > MAX_ROW_DELTA_RATIO:
            errors.append(
                f"Row count changed too much: previous {previous_count}, candidate {candidate_count} ({delta_ratio:.1%} delta)."
            )

        previous_countries = country_counts(previous_rows)
        candidate_countries = country_counts(candidate_rows)
        all_countries = sorted(set(previous_countries) | set(candidate_countries))
        for country in all_countries:
            previous_value = previous_countries.get(country, 0)
            candidate_value = candidate_countries.get(country, 0)
            summary["country_deltas"][country] = {
                "previous": previous_value,
                "candidate": candidate_value,
            }
            if previous_value == 0:
                continue
            absolute_delta = abs(candidate_value - previous_value)
            delta_ratio = absolute_delta / previous_value
            if delta_ratio > MAX_COUNTRY_DELTA and absolute_delta > MAX_COUNTRY_ABSOLUTE_DELTA:
                errors.append(
                    f"Country count changed too much for {country}: previous {previous_value}, candidate {candidate_value} "
                    f"({delta_ratio:.1%}, {absolute_delta} rows delta)."
                )

        previous_last_values = [row.get("last", "").strip() for row in previous_rows if row.get("last", "").strip()]
        candidate_last_values = [row.get("last", "").strip() for row in candidate_rows if row.get("last", "").strip()]
        previous_latest = max(previous_last_values, key=quarter_key) if previous_last_values else ""
        candidate_latest = max(candidate_last_values, key=quarter_key) if candidate_last_values else ""
        summary["latest_last"] = {
            "previous": previous_latest,
            "candidate": candidate_latest,
        }

        if previous_latest and candidate_latest and quarter_key(candidate_latest) < quarter_key(previous_latest):
            errors.append(
                f"Candidate data appears older than the current CSV: previous latest '{previous_latest}', candidate latest '{candidate_latest}'."
            )

    return not errors, errors, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a candidate pubs.csv against the current version.")
    parser.add_argument("candidate", help="Path to the candidate CSV.")
    parser.add_argument("--previous", default="pubs.csv", help="Path to the current CSV.")
    parser.add_argument(
        "--report",
        default="validation-report.json",
        help="Path to write the validation report JSON.",
    )
    args = parser.parse_args()

    candidate_path = Path(args.candidate)
    previous_path = Path(args.previous)
    report_path = Path(args.report)

    previous_rows = load_rows(previous_path) if previous_path.exists() else []
    candidate_rows = load_rows(candidate_path)

    is_valid, errors, summary = validate(previous_rows, candidate_rows)
    report = {
        "valid": is_valid,
        "errors": errors,
        "summary": summary,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not is_valid:
        for error in errors:
            print(error)
        raise SystemExit(1)

    print("CSV validation passed.")


if __name__ == "__main__":
    main()
