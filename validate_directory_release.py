"""Validate every map asset against its permanent identity and pin evidence."""
import argparse
from pathlib import Path
from directory_release import validate_release, write_json


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--allow-review-downgrades', action='store_true')
    parser.add_argument('--allow-expired-evidence', action='store_true')
    args = parser.parse_args()
    try:
        errors = validate_release(args.root, args.previous, args.allow_review_downgrades, args.allow_expired_evidence)
    except Exception as error:
        errors = [f'{type(error).__name__}: {error}']
    if args.report:
        write_json(args.report, {'valid': not errors, 'errors': errors})
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
    print('Complete directory release validation passed')
