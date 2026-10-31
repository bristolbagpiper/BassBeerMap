"""Apply owner decisions only while the reviewed facts still match."""
import copy
from pathlib import Path
from directory_release import read_json, read_rows, write_release


def apply_decisions(registry, decisions):
    changed = False
    for decision in decisions.get('postcode_reviews', []):
        entry = registry.get('venues', {}).get(decision['venue_id']) or {}
        pin = entry.get('verified_pin') or {}
        issue = pin.get('metadata_issue') or {}
        if (entry.get('listing', {}).get('pub_name') != decision.get('pub_name')
                or {k: pin.get(k) for k in ('lat', 'lng')} != decision.get('approved_coordinate')
                or any(issue.get(k) != decision.get(k) for k in ('listed', 'reference', 'source_url'))):
            continue
        review = {k: copy.deepcopy(v) for k, v in decision.items() if k != 'venue_id'}
        if issue.get('owner_review') != review:
            issue['owner_review'] = review
            changed = True
    for decision in decisions.get('trading_reviews', []):
        entry = registry.get('venues', {}).get(decision['venue_id']) or {}
        trading = entry.get('trading_status_review') or {}
        if {k: trading.get(k) for k in ('status', 'reason', 'evidence')} != decision.get('snapshot'):
            continue
        review = {k: copy.deepcopy(v) for k, v in decision.items() if k != 'venue_id'}
        if entry.get('acknowledged_trading_review') != review:
            entry['acknowledged_trading_review'] = review
            changed = True
    return changed


def main():
    root = Path('.')
    registry = read_json(root / 'venue-registry.json')
    if not apply_decisions(registry, read_json(root / 'audit/closed-review-decisions.json')):
        return
    write_release(root, read_rows(root / 'pubs.csv'), registry,
                  read_json(root / 'directory-meta.json'), read_json(root / 'pub-coordinates.json')['coordinates'])


if __name__ == '__main__':
    main()

