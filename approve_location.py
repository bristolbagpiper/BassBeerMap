"""Apply a repository-owner review, with source-checked location evidence."""
import argparse
import copy
import csv
import html
import json
import os
import re
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import unquote_plus, urlparse
from urllib.request import Request, urlopen

from coordinate_sources import fetch_text, name_matches, parse_camra
from directory_release import (
    distance_metres, point_valid, read_json, read_rows, validate_release,
    venue_key, write_json, write_release,
)


VENUE_ID = r"v-[0-9a-f-]{36}"
CAMRA_COMMAND = re.compile(
    rf"^/verify-location (?P<venue_id>{VENUE_ID}) "
    r"(?P<url>https://camra\.org\.uk/pubs/[a-z0-9-]+)$"
)
RETAIN_COMMAND = re.compile(
    rf'^/verify-location (?P<venue_id>{VENUE_ID}) retain '
    r'(?P<lat>-?\d+(?:\.\d+)?) (?P<lng>-?\d+(?:\.\d+)?) '
    r'(?P<url>https://camra\.org\.uk/pubs/[a-z0-9-]+)'
    r'(?: fsa (?P<fsa_id>\d+))?$'
)
GOOGLE_COMMAND = re.compile(
    rf'^/verify-location (?P<venue_id>{VENUE_ID}) google '
    r'(?P<lat>-?\d+(?:\.\d+)?) (?P<lng>-?\d+(?:\.\d+)?) '
    r'(?P<url>https://www\.google\.com/maps/place/\S+) '
    r'"(?P<reference_name>[^"]+)" "(?P<reference_address>[^"]+)"'
    r'(?: camra (?P<corroborating_source_url>https://camra\.org\.uk/pubs/[a-z0-9-]+))?$'
)
MANUAL_CHECK_FIELDS = [
    "venue_key", "lat", "lng", "reference_name", "reference_address",
    "source", "source_url", "corroborating_source_url", "checked_at",
    "review_note",
]


def approved_commands(event, owner):
    comment = event.get("comment", {})
    user = comment.get("user", {})
    if user.get("login") != owner or user.get("type") != "User" or event.get("issue", {}).get("number") != 5:
        raise ValueError("Only the repository owner can approve locations in the coordinate review issue")
    lines = comment.get("body", "").strip().splitlines()
    commands = []
    for line in lines:
        line = line.strip()
        retain = RETAIN_COMMAND.fullmatch(line)
        if retain:
            command = {'kind': 'retain', 'venue_id': retain['venue_id'],
                       'lat': float(retain['lat']), 'lng': float(retain['lng']), 'url': retain['url']}
            if retain['fsa_id']:
                command['fsa_id'] = int(retain['fsa_id'])
            commands.append(command)
            continue
        camra = CAMRA_COMMAND.fullmatch(line)
        if camra:
            commands.append({"kind": "camra", "venue_id": camra["venue_id"], "url": camra["url"]})
            continue
        google = GOOGLE_COMMAND.fullmatch(line)
        if google:
            command = {
                "kind": "google",
                "venue_id": google["venue_id"],
                "lat": float(google["lat"]),
                "lng": float(google["lng"]),
                "url": google["url"],
                "reference_name": google["reference_name"],
                "reference_address": google["reference_address"],
            }
            if google["corroborating_source_url"]:
                command["corroborating_source_url"] = google["corroborating_source_url"]
            commands.append(command)
            continue
        raise ValueError(
            'Use /verify-location <venue-id> <CAMRA-url>, or '
            '/verify-location <venue-id> google <lat> <lng> <Google Maps place URL> '
            '"<place name>" "<full address>" [camra <CAMRA-url>]'
        )
    if not lines or len(lines) > 30:
        raise ValueError("Provide between 1 and 30 approval commands")
    if len({command["venue_id"] for command in commands}) != len(commands):
        raise ValueError("Approve each venue ID only once in a comment")
    return commands


def camra_previous_name_matches(page_text, listing):
    """Only accept an exact former name from CAMRA's Previous Names field."""
    match = re.search(r'<dt\b[^>]*>\s*Previous Names\s*</dt>\s*<dd\b[^>]*>(.*?)</dd>',
                      page_text, re.I | re.S)
    if not match:
        return False
    text = html.unescape(re.sub(r'<[^>]+>', ' ', match.group(1)))
    normalize = lambda value: re.sub(r'[^a-z0-9]', '', value.casefold())
    expected = normalize(row_name) if (row_name := listing.strip()) else ''
    return bool(expected and any(normalize(part) == expected for part in re.split(r'[,;]', text)))


def camra_heading_confirms_listing_alias(listing, source_name, page_text):
    """Require both a directory former name and CAMRA's exact heading alias."""
    match = re.fullmatch(r'\s*(.*?)\s*\(was\s+([^()]+)\)\s*', listing, re.I)
    if not match:
        nickname = re.fullmatch(r'\s*(.*?)\s*\((?!was\s+)([^()]+)\)\s*', listing, re.I)
        if not nickname:
            return False
        primary, alias = nickname.groups()
        if not normalized_place_name(primary) or normalized_place_name(primary) != normalized_place_name(source_name):
            return False
        compact = lambda value: re.sub(r'[^a-z0-9]', '', value.casefold())
        aliases = re.findall(r'\(([^()]*)\)', extract_camra_heading(page_text))
        return bool(compact(alias)) and any(compact(alias) == compact(item) for item in aliases)
    current, former = match.groups()
    if (normalized_place_name(current)
            and normalized_place_name(current) == normalized_place_name(source_name)
            and normalized_place_name(extract_camra_heading(page_text)) == normalized_place_name(source_name)
            and camra_previous_name_matches(page_text, former)):
        return True
    if not normalized_place_name(former) or normalized_place_name(former) != normalized_place_name(source_name):
        return False
    aliases = re.findall(r'\(([^()]*)\)', extract_camra_heading(page_text))
    return any(normalized_place_name(current) == normalized_place_name(alias) for alias in aliases)


def camra_heading_confirms_slash_names(listing, source_name, page_text, place_name):
    """Require a complete primary name and an explicit CAMRA heading alias."""
    listing = re.sub(r'\s*\(PMC\)\s*', '', listing, flags=re.I).strip()
    parts = listing.split('/')
    if len(parts) != 2:
        return False
    names = [manual_name_tokens(part) for part in parts]
    source_primary = manual_name_tokens(source_name.split(',', 1)[0].split(' - ', 1)[0])
    heading = extract_camra_heading(page_text)
    if (not all(names) or source_primary not in names
            or normalized_place_name(heading) != normalized_place_name(source_name)):
        return False
    alternate = names[1] if names[0] == source_primary else names[0]
    locality_tokens = manual_name_tokens(place_name)
    aliases = re.findall(r'\(([^()]*)\)', heading)
    return any(manual_name_tokens(alias) - locality_tokens == alternate for alias in aliases)


def camra_heading_confirms_omitted_arms(listing, source_name, page_text):
    """Accept one omitted Arms suffix only when heading and source agree exactly."""
    wanted = normalized_place_name(listing)
    official = normalized_place_name(source_name)
    return bool(wanted and len(wanted.split()) == 1 and official == wanted + ' arms'
                and normalized_place_name(extract_camra_heading(page_text)) == official)


def camra_heading_confirms_omitted_ye(listing, source_name, page_text):
    """Require the entire remaining name and CAMRA heading to agree."""
    wanted = normalized_place_name(listing)
    official = normalized_place_name(source_name)
    wanted = re.sub(r'^ye olde\b', 'ye old', wanted)
    return bool(wanted.startswith('ye ') and len(official.split()) >= 2
                and wanted[3:] == official
                and normalized_place_name(extract_camra_heading(page_text)) == official)


def fsa_confirms_address_name(row, source, record, expected_id, target=None):
    """Corroborate a name difference only with an exact FSA premises name and street."""
    if not record or record.get('FHRSID') != expected_id:
        return False
    compact = lambda value: re.sub(r'[^a-z0-9]', '', re.sub(r'^the\s+', '', str(value).casefold().strip()))
    listing = compact(row['pub_name'])
    premises = compact(record.get('AddressLine1', ''))
    street = compact(source.get('address', ''))
    fsa_streets = [compact(record.get(f'AddressLine{i}', '')) for i in range(2, 5)]
    fsa_name = normalized_place_name(record.get('BusinessName', ''))
    camra_name = normalized_place_name(source.get('name', ''))
    if (listing and premises == listing and street and street in fsa_streets
            and fsa_name and fsa_name == camra_name and record.get('PostCode')):
        return True
    # Some official records add a locality to the full business name while
    # CAMRA uses the chain's shorter name. Require the exact FSA listing name,
    # postcode and point, plus the same street and a complete name prefix.
    listed_name = normalized_place_name(row['pub_name'])
    source_street = compact(re.sub(r'^\s*\d+\s+', '', source.get('address', '')))
    try:
        fsa_point = {'lat': float(record['geocode']['latitude']), 'lng': float(record['geocode']['longitude'])}
    except (KeyError, TypeError, ValueError):
        return False
    postcode = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    return bool(target and point_valid(fsa_point) and distance_metres(target, fsa_point) <= 1
                and postcode(record.get('PostCode')) == postcode(row['postcode'])
                and fsa_name == listed_name and len(camra_name.split()) >= 2
                and listed_name.startswith(camra_name + ' ')
                and source_street and source_street in fsa_streets)


def validate_retained_point(command, row, entry, source, camra_page_text='', fsa_record=None):
    pin = entry.get('verified_pin') or {}
    target = {'lat': command['lat'], 'lng': command['lng']}
    if not point_valid(pin) or distance_metres(pin, target) > 0.01:
        raise ValueError('The reviewed point no longer matches the saved venue point')
    if not any(e.get('source') == 'camra' and e.get('url') == command['url'] for e in pin.get('evidence', [])):
        raise ValueError('CAMRA source is not part of the saved venue evidence')
    compact = lambda value: re.sub(r'[^A-Z0-9]', '', str(value).upper())
    if not source.get('postcode') or compact(source['postcode']) != compact(row['postcode']):
        raise ValueError('CAMRA postcode does not match the directory')
    locality = str(source.get('locality') or '').strip().casefold()
    listing_locality = str(row.get('place_name') or '').strip().casefold()
    if ((not source.get('address') and not (locality and locality == listing_locality))
            or not point_valid(source) or distance_metres(pin, source) > 50):
        raise ValueError('CAMRA address or coordinates do not corroborate the saved point')
    tokens = lambda value: set(re.findall(r'[a-z0-9]+', value.casefold())) - {'the', 'at', 'and', 'inn', 'pub', 'bar', 'hotel'}
    listing_tokens = tokens(re.sub(r'\s*\(PMC\)\s*', ' ', row['pub_name'], flags=re.I))
    if ((len(listing_tokens) < 2 or not listing_tokens.issubset(tokens(source.get('name', ''))))
            and not camra_previous_name_matches(camra_page_text, row['pub_name'])
            and not camra_heading_confirms_listing_alias(row['pub_name'], source.get('name', ''), camra_page_text)
            and not camra_heading_confirms_slash_names(row['pub_name'], source.get('name', ''), camra_page_text, row.get('place_name', ''))
            and not camra_heading_confirms_omitted_arms(row['pub_name'], source.get('name', ''), camra_page_text)
            and not camra_heading_confirms_omitted_ye(row['pub_name'], source.get('name', ''), camra_page_text)
            and not fsa_confirms_address_name(row, source, fsa_record, command.get('fsa_id'), target)):
        raise ValueError('CAMRA name does not identify the listed venue')
    return pin


def manual_name_matches(listing, reference):
    # Reviewed bilingual listing: CAMRA and Google use the English name;
    # the Welsh name is independently listed at the same address by the FSA.
    if (listing.strip().casefold() == "pelican inn/tafarn y pelican"
            and reference.strip().casefold() == "the pelican inn"):
        return True
    generic = {
        "the", "bar", "pub", "inn", "hotel", "tavern", "restaurant",
        "brewery", "taproom", "club", "social", "house", "public",
    }
    tokens = lambda value: {
        token for token in re.findall(r"[a-z0-9]+", re.sub(r"\bwmc\b", "working mens", value.casefold()))
        if token not in generic and not token.isdigit()
    }
    normalized_listing = re.sub(r"\s*\(PMC\)", "", listing, flags=re.I)
    # Parenthetical text often identifies a club or alias, while Google uses
    # the venue's primary name. Keep the primary listing name as the match key.
    primary_name = normalized_listing.split("(", 1)[0].strip()
    wanted = tokens(primary_name)
    actual = tokens(reference.split(",")[0].split(" - ")[0])
    if wanted and wanted.issubset(actual):
        return True
    # Some named-place listings omit a final "Arms" from the directory name.
    # Accept that one exact suffix omission without loosening other name matches.
    without_arms = wanted - {"arms"}
    return (
        bool(without_arms)
        and primary_name.casefold().endswith(" arms")
        and actual == without_arms
    )


def manual_name_tokens(value):
    generic = {
        "the", "bar", "pub", "inn", "hotel", "tavern", "restaurant",
        "brewery", "taproom", "club", "social", "house", "public",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in generic and not token.isdigit()
    }


def normalized_place_name(value):
    primary = value.split("(", 1)[0].split(",", 1)[0].split(" - ", 1)[0].strip()
    tokens = re.findall(r"[a-z0-9]+", primary.casefold())
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    # CAMRA often omits a generic final "Inn" used by the named Google place.
    if len(tokens) > 1 and tokens[-1] == "inn":
        tokens = tokens[:-1]
    return " ".join(tokens)


def extract_camra_heading(page_text):
    if not isinstance(page_text, str):
        return ""
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", page_text, re.I | re.S)
    if not match:
        return ""
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def camra_heading_matches_listing(listing, page_text):
    """Match only a complete listing name explicitly shown as a heading alias."""
    listing = re.sub(r'\s*\(PMC\)\s*', '', listing, flags=re.I).strip()
    wanted = normalized_place_name(listing)
    aliases = re.findall(r'\(([^()]*)\)', extract_camra_heading(page_text))
    return bool(wanted) and any(normalized_place_name(alias) == wanted for alias in aliases)


def camra_documents_co_located_companion(page_text, listing, reference_name):
    """Check that CAMRA explicitly places extra listing-name words at this venue."""
    normalized_listing = re.sub(r"\s*\(PMC\)", "", listing, flags=re.I)
    listing_primary = normalized_listing.split("(", 1)[0].strip()
    listing_words = [
        token for token in re.findall(r"[a-z0-9]+", listing_primary.casefold())
        if token not in {"the", "bar", "pub", "inn", "hotel", "tavern", "restaurant",
                         "brewery", "taproom", "club", "social", "house", "public",
                         "and", "or"}
        and not token.isdigit()
    ]
    reference_words = manual_name_tokens(reference_name)
    extra_words = [token for token in listing_words if token not in reference_words]
    if not extra_words:
        return False
    phrase = " ".join(extra_words)

    without_scripts = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>", " ", page_text,
        flags=re.I | re.S,
    )
    plain_text = html.unescape(re.sub(r"<[^>]+>", " ", without_scripts))
    for sentence in re.split(r"[.!?]+", plain_text):
        normalized_sentence = " ".join(re.findall(r"[a-z0-9]+", sentence.casefold()))
        if phrase not in normalized_sentence:
            continue
        if re.search(
            r"\b(?:located|situated)\s+(?:upstairs|above|inside|within)\b|\bupstairs\b",
            normalized_sentence,
        ):
            return True
    return False


def validate_camra_corroboration(
    command, row, source, heading, require_alias, camra_page_text=""
):
    compact = lambda value: re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if not source.get("postcode") or compact(source["postcode"]) != compact(row["postcode"]):
        raise ValueError("CAMRA corroboration must have the listing's exact full postcode")
    if distance_metres(
        {"lat": command["lat"], "lng": command["lng"]}, source
    ) > 50:
        raise ValueError("CAMRA corroboration must be within 50 metres of the Google place marker")

    google_name = normalized_place_name(command["reference_name"])
    camra_name = normalized_place_name(source.get("name", ""))
    heading_name = normalized_place_name(heading)
    if not google_name or google_name != camra_name or camra_name != heading_name:
        raise ValueError("CAMRA page, structured venue, and Google place must have the same primary name")

    normalized_listing = re.sub(r"\s*\(PMC\)", "", row["pub_name"], flags=re.I)
    listing_primary = normalized_listing.split("(", 1)[0].strip()
    listing_tokens = manual_name_tokens(listing_primary)
    google_tokens = manual_name_tokens(command["reference_name"])
    missing = listing_tokens - google_tokens
    if require_alias:
        aliases = re.findall(r"\(([^()]*)\)", heading)
        alias_tokens = set().union(*(manual_name_tokens(alias) for alias in aliases)) if aliases else set()
        documented_companion = camra_documents_co_located_companion(
            camra_page_text, row["pub_name"], command["reference_name"]
        )
        if (
            not missing
            or (
                not missing.issubset(alias_tokens)
                and not documented_companion
            )
            or not listing_tokens.issubset(google_tokens | alias_tokens | (
                listing_tokens if documented_companion else set()
            ))
        ):
            raise ValueError(
                "CAMRA page does not document every missing listing-name word "
                "as an alias or co-located venue"
            )
    return True


def validate_google_place(
    command, row, centre, camra_source=None, camra_heading="", camra_page_text=""
):
    """Require a named-place marker, matching address, and local coordinates."""
    parsed = urlparse(command["url"])
    if parsed.scheme != "https" or parsed.netloc != "www.google.com" or not parsed.path.startswith("/maps/place/"):
        raise ValueError("Google review must link to a named Google Maps place")
    slug = unquote_plus(parsed.path.split("/maps/place/", 1)[1].split("/@", 1)[0])
    if re.sub(r"[^a-z0-9]", "", slug.casefold()) != re.sub(
        r"[^a-z0-9]", "", command["reference_name"].casefold()
    ):
        raise ValueError("Reference name does not match the Google Maps place URL")
    name_matches_listing = manual_name_matches(row["pub_name"], command["reference_name"])
    if not name_matches_listing:
        if camra_source is None:
            raise ValueError("Google place name does not identify the listed venue")
        validate_camra_corroboration(
            command, row, camra_source, camra_heading, require_alias=True,
            camra_page_text=camra_page_text,
        )
    elif camra_source is not None:
        validate_camra_corroboration(command, row, camra_source, camra_heading, require_alias=False)
    compact = lambda value: re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if compact(row["postcode"]) not in compact(command["reference_address"]):
        raise ValueError("Google reference address must contain the listing's full postcode")
    if not point_valid({"lat": command["lat"], "lng": command["lng"]}):
        raise ValueError("Google location coordinates are invalid")
    place_points = re.findall(
        r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",
        command["url"],
    )
    target = {"lat": command["lat"], "lng": command["lng"]}
    if not place_points or min(
        distance_metres(target, {"lat": float(lat), "lng": float(lng)})
        for lat, lng in place_points
    ) > 1:
        raise ValueError("Coordinates must match the named Google Maps place marker")
    if distance_metres(target, centre) > 6000:
        raise ValueError("Google place is more than 6 km from the listed area; requires address review")
    return not name_matches_listing


def stage_manual_checks(root, staging, reviews):
    """Keep the manually accepted point and its production override in sync."""
    root, staging = Path(root), Path(staging)
    checks_path = root / "audit/manual-place-checks.csv"
    if checks_path.exists():
        with checks_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or MANUAL_CHECK_FIELDS
            rows = list(reader)
    else:
        fieldnames, rows = MANUAL_CHECK_FIELDS, []
    overrides_payload = read_json(root / "venue-coordinate-overrides.json", {"venues": {}})
    overrides = overrides_payload.setdefault("venues", {})
    generated = read_json(root / "venue-coordinates.json", {"venues": {}}).get("venues", {})

    for review in reviews:
        key = review["venue_key"]
        lat, lng = review["lat"], review["lng"]
        previous = overrides.get(key) or generated.get(key)
        audit_row = {
            "venue_key": key,
            "lat": str(lat),
            "lng": str(lng),
            "reference_name": review["reference_name"],
            "reference_address": review["reference_address"],
            "source": "google-maps-named-place",
            "source_url": review["source_url"],
            "corroborating_source_url": review.get("corroborating_source_url", ""),
            "checked_at": review["checked_at"],
            "review_note": (
                "Repository owner confirmed the named-place marker and full address in issue #5; "
                "CAMRA corroborated the primary name, documented alias or co-located venue label, "
                "postcode, and nearby point."
                if review.get("corroborating_source_url")
                else "Repository owner confirmed the named-place marker and full address in issue #5."
            ),
        }
        existing = next((index for index, row in enumerate(rows) if row.get("venue_key") == key), None)
        if existing is None:
            rows.append(audit_row)
        else:
            rows[existing] = audit_row

        override = {
            "lat": lat,
            "lng": lng,
            "source": audit_row["source"],
            "source_url": audit_row["source_url"],
            "checked_at": audit_row["checked_at"],
            "audit_reason": "manual_named_place_address_and_map_check",
            "reference_name": audit_row["reference_name"],
            "reference_address": audit_row["reference_address"],
            "review_note": audit_row["review_note"],
            "corroborating_source_url": audit_row["corroborating_source_url"],
        }
        if previous:
            override.update({
                "previous_lat": float(previous["lat"]),
                "previous_lng": float(previous["lng"]),
                "previous_source": previous.get("source", "venue"),
                "previous_difference_metres": round(
                    distance_metres(previous, {"lat": lat, "lng": lng}), 1
                ),
            })
        else:
            override["previous_source"] = "postcode"
        overrides[key] = override

    staging.mkdir(parents=True, exist_ok=True)
    write_json(staging / "venue-coordinate-overrides.json", overrides_payload)
    audit_staging = staging / "audit"
    audit_staging.mkdir(parents=True, exist_ok=True)
    with (audit_staging / "manual-place-checks.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def approve(root, staging, event, owner):
    commands = approved_commands(event, owner)
    root, staging = Path(root), Path(staging)
    errors = validate_release(root)
    if errors:
        raise ValueError("; ".join(errors))
    rows = read_rows(root / "pubs.csv")
    active = {r["venue_id"]: r for r in rows}
    registry = copy.deepcopy(read_json(root / "venue-registry.json"))
    ledger = read_json(root / "coordinate-verification.json")
    manual_reviews = []

    for command in commands:
        identifier = command["venue_id"]
        if identifier not in active:
            raise ValueError("Venue ID is absent from the current directory")
        row, entry = active[identifier], registry["venues"][identifier]
        if any(r.get("comment_id") == event["comment"]["id"] for r in entry.get("location_approvals", [])):
            continue
        if ledger["records"][entry["current_key"]]["display_precision"] == "venue":
            raise ValueError("Only an unverified location can be approved; verified relocations require a dedicated review")
        centre = read_json(root / "pub-coordinates.json")["coordinates"][row["postcode"]]

        if command["kind"] == "retain":
            url = command['url']
            camra_page = fetch_text(url)
            source = parse_camra(camra_page)
            fsa_record = None
            if command.get('fsa_id'):
                request = Request(f"https://api.ratings.food.gov.uk/Establishments/{command['fsa_id']}",
                                  headers={'User-Agent': 'BassBeerMap location review', 'Accept': 'application/json', 'x-api-version': '2'})
                with urlopen(request, timeout=20) as response:
                    fsa_record = json.load(response)
                if not fsa_confirms_address_name(row, source, fsa_record, command['fsa_id'],
                                                 {'lat': command['lat'], 'lng': command['lng']}):
                    raise ValueError('FSA record does not establish the venue name and street relationship')
            pin = copy.deepcopy(validate_retained_point(command, row, entry, source, camra_page, fsa_record))
            pin.pop('review_required', None)
            pin.pop('review_reason', None)
            if fsa_record:
                pin['name_corroboration'] = {'source': 'food-standards-agency',
                    'url': f"https://ratings.food.gov.uk/business/{command['fsa_id']}",
                    'checked_at': date.today().isoformat(), 'business_name': fsa_record['BusinessName'],
                    'premises_name': fsa_record['AddressLine1'], 'postcode': fsa_record['PostCode']}
            if fsa_record and re.sub(r'\s+', '', fsa_record['PostCode']).upper() != re.sub(r'\s+', '', row['postcode']).upper():
                pin['metadata_issue'] = {'field': 'postcode', 'listed': row['postcode'],
                    'reference': fsa_record['PostCode'], 'source_url': f"https://ratings.food.gov.uk/business/{command['fsa_id']}"}
            pin['evidence'].insert(0, dict(source='camra', url=url, checked_at=date.today().isoformat(),
                match='owner_confirmed_saved_point_corroborated_by_named_camra_venue',
                reference_coordinate={'lat': source['lat'], 'lng': source['lng']},
                reference_name=source['name'], reference_address=source['address'],
                reference_postcode=source['postcode'], reviewed_by=owner, review_url=event['comment']['html_url']))
            approval_url = url
        elif command["kind"] == "camra":
            url = command["url"]
            camra_page = fetch_text(url)
            source = parse_camra(camra_page)
            proposals = entry.get("location_research", {}).get("proposals", [])
            proposed = next((p for p in proposals if p["url"] == url), None)
            if not name_matches(row["pub_name"], source["name"], row.get("place_name", "")):
                exact_postcode = re.sub(r'\s+', '', source.get('postcode', '')).upper() == re.sub(r'\s+', '', row['postcode']).upper()
                heading_alias = bool(exact_postcode and camra_heading_matches_listing(row['pub_name'], camra_page))
                if not heading_alias and (not proposed or not (proposed["name"] == source["name"] or name_matches(proposed["name"], source["name"], row.get("place_name", "")))):
                    raise ValueError("Source name does not identify this pub or a recorded research candidate")
            if proposed and (distance_metres(source, proposed) > 50 or (proposed["postcode"] and source["postcode"] != proposed["postcode"])):
                raise ValueError("Source address/coordinate changed since research; refresh the candidate before approving")
            if distance_metres(source, centre) > 6000:
                raise ValueError("Source is more than 6 km from the listed area; requires a dedicated relocation/address review")
            locality = source.get("locality", "").strip()
            listing_locality = row.get("place_name", "").strip()
            locality_matches = bool(locality and listing_locality and locality.casefold() == listing_locality.casefold())
            if not source.get("postcode") or (not source.get("address") and not locality_matches):
                raise ValueError("Source lacks a named street address or matching locality and full postcode")
            pin = dict(lat=source["lat"], lng=source["lng"], source="owner-reviewed-camra", evidence=[dict(
                source="camra", url=url, checked_at=date.today().isoformat(),
                match=("owner_reviewed_named_address_and_map" if source.get("address") else "owner_reviewed_named_locality_and_map"),
                reference_coordinate=dict(lat=source["lat"], lng=source["lng"]), reference_name=source["name"],
                reference_address=source.get("address", ""), reference_locality=locality,
                reference_postcode=source["postcode"],
                reviewed_by=owner, review_url=event["comment"]["html_url"])])
            if row["postcode"].replace(" ", "").upper() != source["postcode"].replace(" ", "").upper():
                pin["metadata_issue"] = dict(field="postcode", listed=row["postcode"], reference=source["postcode"], source_url=url)
            source_name = source["name"].split(",")[0].split(" - ")[0].strip()
            for name in (row["pub_name"], source_name):
                key = venue_key(dict(row, pub_name=name, postcode=source["postcode"]))
                if key not in entry["aliases"]:
                    entry["aliases"].append(key)
            approval_url = url
        else:
            camra_source = None
            camra_heading = ""
            camra_page = ""
            corroborating_url = command.get("corroborating_source_url", "")
            if corroborating_url:
                camra_page = fetch_text(corroborating_url)
                camra_source = parse_camra(camra_page)
                camra_heading = extract_camra_heading(camra_page)
            camra_alias_used = validate_google_place(
                command, row, centre, camra_source, camra_heading, camra_page
            )
            checked_at = date.today().isoformat()
            reference_coordinate = {"lat": command["lat"], "lng": command["lng"]}
            pin = dict(
                lat=command["lat"], lng=command["lng"], source="owner-reviewed-google-maps",
                evidence=[dict(
                    source="google-maps-named-place",
                    url=command["url"],
                    checked_at=checked_at,
                    match="owner_reviewed_named_place_and_full_postcode",
                    reference_coordinate=reference_coordinate,
                    reference_name=command["reference_name"],
                    reference_address=command["reference_address"],
                    reference_postcode=row["postcode"],
                    reviewed_by=owner,
                    review_url=event["comment"]["html_url"],
                )],
            )
            if camra_source is not None:
                camra_point = {"lat": camra_source["lat"], "lng": camra_source["lng"]}
                pin["evidence"].append(dict(
                    source="camra",
                    url=corroborating_url,
                    checked_at=checked_at,
                    match=(
                        "owner_reviewed_documented_name_relationship_same_postcode_and_within_50m"
                        if camra_alias_used
                        else "owner_reviewed_same_primary_name_same_postcode_and_within_50m"
                    ),
                    reference_coordinate=camra_point,
                    reference_name=camra_source["name"],
                    reference_address=camra_source.get("address", ""),
                    reference_locality=camra_source.get("locality", ""),
                    reference_postcode=camra_source["postcode"],
                    reviewed_by=owner,
                    review_url=event["comment"]["html_url"],
                ))
            approval_url = command["url"]
            manual_reviews.append({
                "venue_key": entry["current_key"],
                "lat": command["lat"],
                "lng": command["lng"],
                "reference_name": command["reference_name"],
                "reference_address": command["reference_address"],
                "source_url": command["url"],
                "corroborating_source_url": corroborating_url,
                "checked_at": checked_at,
            })

        previous = copy.deepcopy(entry.get("verified_pin"))
        entry["verified_pin"] = pin
        entry.setdefault("location_approvals", []).append(dict(
            comment_id=event["comment"]["id"],
            review_url=event["comment"]["html_url"],
            reviewed_by=owner,
            checked_at=date.today().isoformat(),
            source_url=approval_url,
            previous_pin=previous,
        ))

    staging.mkdir(parents=True, exist_ok=True)
    write_release(staging, rows, registry, read_json(root / "directory-meta.json"), read_json(root / "pub-coordinates.json")["coordinates"])
    # Keep non-release audit inputs available to the candidate validators.
    overrides_path = root / "venue-coordinate-overrides.json"
    if overrides_path.exists():
        shutil.copyfile(overrides_path, staging / "venue-coordinate-overrides.json")
    else:
        write_json(staging / "venue-coordinate-overrides.json", {"venues": {}})
    (staging / "audit").mkdir(parents=True, exist_ok=True)
    checks_path = root / "audit/manual-place-checks.csv"
    if checks_path.exists():
        shutil.copyfile(checks_path, staging / "audit/manual-place-checks.csv")
    else:
        with (staging / "audit/manual-place-checks.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANUAL_CHECK_FIELDS, lineterminator="\n")
            writer.writeheader()
    if manual_reviews:
        stage_manual_checks(root, staging, manual_reviews)
    shutil.copyfile(root / "latest-bass-directory.pdf", staging / "latest-bass-directory.pdf")
    errors = validate_release(staging, root)
    if errors:
        raise ValueError("; ".join(errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--staging", default="candidate-approval")
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    approve(args.root, args.staging, event, os.environ["GITHUB_REPOSITORY_OWNER"])


