"""Apply an explicit repository-owner review, with fresh factual source checks."""
import argparse
import copy
import json
import os
import re
import shutil
from datetime import date
from pathlib import Path

from coordinate_sources import fetch_text, name_matches, parse_camra
from directory_release import (distance_metres, read_json, read_rows, validate_release, venue_key, write_release)


COMMAND = re.compile(r'^/verify-location (v-[0-9a-f-]{36}) (https://camra\.org\.uk/pubs/[a-z0-9-]+)\s*$')


def approved_commands(event, owner):
    comment = event.get('comment', {})
    user = comment.get('user', {})
    if user.get('login') != owner or user.get('type') != 'User' or event.get('issue', {}).get('number') != 5:
        raise ValueError('Only the repository owner can approve locations in the coordinate review issue')
    lines = comment.get('body', '').strip().splitlines()
    matches = [COMMAND.fullmatch(line.strip()) for line in lines]
    if not lines or not all(matches) or len(lines) > 30:
        raise ValueError('Use one /verify-location <venue-id> <CAMRA-url> command per line, with no other text')
    if len({match[1] for match in matches}) != len(matches):
        raise ValueError('Approve each venue ID only once in a comment')
    return [(match[1], match[2]) for match in matches]


def approve(root, staging, event, owner):
    commands = approved_commands(event, owner)
    root, staging = Path(root), Path(staging)
    errors = validate_release(root)
    if errors:
        raise ValueError('; '.join(errors))
    rows = read_rows(root / 'pubs.csv')
    active = {r['venue_id']: r for r in rows}
    registry = copy.deepcopy(read_json(root / 'venue-registry.json'))
    ledger = read_json(root / 'coordinate-verification.json')
    for identifier, url in commands:
        if identifier not in active:
            raise ValueError('Venue ID is absent from the current directory')
        row, entry = active[identifier], registry['venues'][identifier]
        if any(r.get('comment_id') == event['comment']['id'] for r in entry.get('location_approvals', [])):
            continue
        if ledger['records'][entry['current_key']]['display_precision'] == 'venue':
            raise ValueError('Only an unverified location can be approved; verified relocations require a dedicated review')
        source = parse_camra(fetch_text(url))
        proposals = entry.get('location_research', {}).get('proposals', [])
        proposed = next((p for p in proposals if p['url'] == url), None)
        if not name_matches(row['pub_name'], source['name']):
            if not proposed or not (proposed['name'] == source['name'] or name_matches(proposed['name'], source['name'])):
                raise ValueError('Source name does not identify this pub or a recorded research candidate')
        if proposed and (distance_metres(source, proposed) > 50 or (proposed['postcode'] and source['postcode'] != proposed['postcode'])):
            raise ValueError('Source address/coordinate changed since research; refresh the candidate before approving')
        centre = read_json(root / 'pub-coordinates.json')['coordinates'][row['postcode']]
        if distance_metres(source, centre) > 6000:
            raise ValueError('Source is more than 6 km from the listed area; requires a dedicated relocation/address review')
        if not source['address'] or not source['postcode']:
            raise ValueError('Source lacks a named address and full postcode')
        previous = copy.deepcopy(entry.get('verified_pin'))
        pin = dict(lat=source['lat'], lng=source['lng'], source='owner-reviewed-camra', evidence=[dict(
            source='camra', url=url, checked_at=date.today().isoformat(), match='owner_reviewed_named_address_and_map',
            reference_coordinate=dict(lat=source['lat'], lng=source['lng']), reference_name=source['name'],
            reference_address=source['address'], reference_postcode=source['postcode'],
            reviewed_by=owner, review_url=event['comment']['html_url'])])
        if row['postcode'].replace(' ', '').upper() != source['postcode'].replace(' ', '').upper():
            pin['metadata_issue'] = dict(field='postcode', listed=row['postcode'], reference=source['postcode'], source_url=url)
        entry['verified_pin'] = pin
        # The reviewed address remains the same venue if a later PDF corrects
        # its postcode or adopts the independently reviewed source name.
        source_name = source['name'].split(',')[0].split(' - ')[0].strip()
        for name in (row['pub_name'], source_name):
            key = venue_key(dict(row, pub_name=name, postcode=source['postcode']))
            if key not in entry['aliases']:
                entry['aliases'].append(key)
        entry.setdefault('location_approvals', []).append(dict(comment_id=event['comment']['id'], review_url=event['comment']['html_url'],
                                                              reviewed_by=owner, checked_at=date.today().isoformat(), source_url=url,
                                                              previous_pin=previous))
    staging.mkdir(parents=True, exist_ok=True)
    write_release(staging, rows, registry, read_json(root / 'directory-meta.json'), read_json(root / 'pub-coordinates.json')['coordinates'])
    shutil.copyfile(root / 'latest-bass-directory.pdf', staging / 'latest-bass-directory.pdf')
    errors = validate_release(staging, root)
    if errors:
        raise ValueError('; '.join(errors))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--staging', default='candidate-approval')
    args = parser.parse_args()
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    approve(args.root, args.staging, event, os.environ['GITHUB_REPOSITORY_OWNER'])
