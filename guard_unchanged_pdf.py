"""Stop an unchanged source PDF from silently changing published listings."""
import argparse
import csv
import hashlib
import subprocess
from collections import Counter
from pathlib import Path


def listing_counts(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return Counter(tuple(sorted((k, v) for k, v in row.items() if k != 'venue_id'))
                       for row in csv.DictReader(handle))


def needs_approval(old_pdf, new_pdf, old_csv, new_csv):
    return (hashlib.sha256(old_pdf).digest() == hashlib.sha256(new_pdf).digest()
            and listing_counts(old_csv) != listing_counts(new_csv))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-reindex', action='store_true')
    parser.add_argument('--source-pdf', type=Path, default=Path('latest-bass-directory.pdf'))
    parser.add_argument('--baseline-ref', default='HEAD')
    parser.add_argument('--candidate-csv', type=Path, default=Path('candidate-release/pubs.csv'))
    parser.add_argument('--published-csv', type=Path, default=Path('pubs.csv'))
    args = parser.parse_args(argv)
    previous = subprocess.run(['git', 'show', f'{args.baseline_ref}:latest-bass-directory.pdf'],
                              capture_output=True, check=False)
    if previous.returncode:
        raise SystemExit('Cannot read the previously published PDF; refusing to publish.')
    if needs_approval(previous.stdout, args.source_pdf.read_bytes(),
                      args.published_csv, args.candidate_csv) and not args.allow_reindex:
        raise SystemExit('Unchanged PDF produced listing changes. Review the candidate and explicitly run the workflow with reindex approval.')
    print('Unchanged PDF publication guard passed.')


if __name__ == '__main__':
    main()

