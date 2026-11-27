"""Resume priority Google checks for an unchanged directory PDF."""
import copy
import os
from pathlib import Path

from directory_release import build_bundle, read_json, read_rows, write_json


ROOT = Path('.')


def main():
    rows = read_rows(ROOT / 'pubs.csv')
    registry_path = ROOT / 'venue-registry.json'
    original = read_json(registry_path)
    registry = copy.deepcopy(original)
    metadata = read_json(ROOT / 'directory-meta.json')
    postcodes = read_json(ROOT / 'pub-coordinates.json')['coordinates']
    ledger = read_json(ROOT / 'coordinate-verification.json')
    notices = read_json(ROOT / 'trading-notices.json')

    from google_listings import check_google
    results = check_google(rows, registry, rows, pending_only=True)
    if results:
        print('Google resume results:', results)

    google_fields = {'google_listing', 'google_check', 'google_priority_pending', 'google_conflict'}
    for identifier, old_entry in original['venues'].items():
        new_entry = registry['venues'].get(identifier, {})
        if ({key: value for key, value in old_entry.items() if key not in google_fields} !=
                {key: value for key, value in new_entry.items() if key not in google_fields}):
            raise ValueError(f'{identifier}: Google resume changed non-Google registry fields')

    # Build the durable visitor bundle and refresh the persisted review queue
    # only after Google state has made a durable change.
    from location_review import build_review
    if registry == original:
        write_output('progress', False)
        return
    registry['location_review'] = build_review(rows, registry, ledger)
    bundle = build_bundle(rows, metadata, postcodes, ledger, notices, registry)
    previous_bundle = read_json(ROOT / 'directory-release.json')
    def without_links(data):
        public = {key: value for key, value in data.items() if key not in ('locations', 'release_id')}
        public['locations'] = {key: {k:v for k,v in loc.items() if k != 'google_listing'}
                               for key, loc in data.get('locations', {}).items()}
        return public
    if without_links(bundle) != without_links(previous_bundle):
        raise ValueError('Google continuation changed directory content outside business links')
    review = registry['location_review']
    changed = {
        'registry': registry != original,
        'release': bundle != previous_bundle,
        'review': review != original.get('location_review'),
    }
    if not any(changed.values()):
        write_output('progress', False)
        return
    if changed['registry']:
        write_json(registry_path, registry)
    if changed['release']:
        write_json(ROOT / 'directory-release.json', bundle)
    if changed['review']:
        write_json(ROOT / 'location-review.json', review)
    write_output('progress', True)
    write_output('visitor_changed', changed['release'])
    write_output('review_changed', changed['review'])


def write_output(key, value):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')


if __name__ == '__main__':
    main()
