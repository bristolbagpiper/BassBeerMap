"""Select actionable discrepancies from an actual named Google business panel."""
import re
from urllib.parse import unquote, urlencode, urlsplit

from directory_release import active_evidence, directions_address, distance_metres, evidence_valid, point_valid, reviewed_trading_name
from google_listings import IDENTITY, business_profile, identify, material_identity, norm
from hermes_google_browser import _point_from_url, _postcode_in_text


def adjudicate_google_conflict(entry, row, outcome, observations, previous=None):
    pin = entry.get('verified_pin') or {}
    anchor = material_identity(row, entry)
    existing = entry.get('google_conflict') or {}
    if outcome == 'named_listing' or (existing and existing.get('anchor') != anchor):
        entry.pop('google_conflict', None)
    if outcome == 'named_listing':
        return
    # A postcode centre or an uncorroborated Google pin is no basis for an owner alert.
    independent = [e for e in active_evidence(pin) if 'google' not in
                   (str(e.get('source', '')) + str(e.get('url', ''))).lower()]
    supported_pin = evidence_valid(dict(pin, evidence=independent), allow_expired=True)
    precision = (pin.get('premises_review') or {}).get('precision')
    for requested, page in reversed(observations):
        url = page.get('url', '')
        parsed = urlsplit(url)
        titles = page.get('h1') or []
        addresses = page.get('addressButtons') or []
        point = _point_from_url(url)
        if (parsed.scheme != 'https' or parsed.hostname not in ('www.google.com','www.google.co.uk')
                or not parsed.path.startswith('/maps/place/') or not point_valid(point)
                or not page.get('namedBusinessPanel') or not business_profile(page)
                or len(titles) != 1 or len(addresses) != 1):
            continue
        ids = set(re.findall(r'!1s0x[\da-f]+:0x([\da-f]{1,16})(?=!|/|$)', unquote(parsed.path), re.I))
        if len(ids) != 1 or not (cid := int(ids.pop(), 16)):
            continue
        link = f'https://www.google.com/maps?cid={cid}'
        title = ' '.join(titles[0].split())
        address = ' '.join(addresses[0].get('label','').removeprefix('Address:').split())
        if not title or not address:
            continue
        gap = distance_metres(pin,point) if supported_pin else None
        known = previous or entry.get('google_listing') or {}
        expectations = dict(pin, confirmed_google_name=known.get('matched_name'))
        if alternate := reviewed_trading_name(row, entry):
            expectations['alternate_name'] = alternate
        panel_outcome, _ = identify(row, expectations, page)
        # Allow for multiple entrances and large sites. A genuine name and
        # full postcode match are already required for a pin_conflict.
        if outcome == 'shared_listing' and panel_outcome == 'named_listing':
            category = 'identity'
            detail = f'The confirmed Google business {title} is also linked to a different directory premises. Check which establishment owns this link.'
        elif panel_outcome == 'pin_conflict' and gap is not None and gap >= (250 if precision == 'building' else 500):
            category = 'location'
            detail = f'Google places {title} about {round(gap / 10) * 10:,} m from the independently supported pin. Compare the two premises.'
        elif panel_outcome == 'identity_conflict':
            same_postcode = any(_postcode_in_text(str(pc).replace(' ', ''),address) for pc in
                                (row['postcode'], (pin.get('metadata_issue') or {}).get('reference')) if pc)
            names = [row['pub_name'], expectations.get('alternate_name'), known.get('matched_name')]
            same_name = any(norm(n) == norm(title) for n in names if n)
            similar_name = any(norm(n) in norm(title) or norm(title) in norm(n) for n in names if n)
            bound = bool(requested and (
                (known.get('maps_url') and requested.get('maps_url') == known['maps_url']) or
                (known.get('place_id') and requested.get('place_id') == known['place_id'])))
            supported = directions_address(dict(pin, postcode=row['postcode'], evidence=independent)) or {}
            street = supported.get('street','')
            # For an unbound search, a similar name/town is insufficient. Require the
            # same numbered street address AND an independently identified building.
            numbered = re.match(r'^\d+[a-z]?\b', street, re.I) and norm(street) in norm(address)
            if (not similar_name and same_postcode and gap is not None and gap <= 50 and
                    (bound or (precision == 'building' and numbered))):
                category = 'identity'
                detail = f'The Google business at the directory premises is named {title}. Confirm which business the listing should identify.'
            elif bound and not similar_name and not same_postcode and (gap is None or gap >= 250):
                category = 'identity'
                detail = f'The previously verified link opens {title} at a different address. Confirm which business belongs to this directory entry.'
            elif bound and same_name and not same_postcode and gap is not None and gap >= 500:
                category = 'location'
                detail = f'The previously verified Google business now gives a different address about {round(gap / 10) * 10:,} m from the saved pin. Check whether it moved.'
            else:
                continue
        else:
            continue
        query = f"{pin['lat']},{pin['lng']}" if supported_pin else ', '.join(row[k] for k in IDENTITY)
        finding = dict(kind=category, anchor=anchor, title=title, address=address, maps_url=link,
                       pin_url='https://www.google.com/maps/search/?' + urlencode({'api':1,'query':query}),
                       detail=detail)
        # Small provider geometry changes do not create a new owner question.
        if any(existing.get(k) != finding[k] for k in ('kind','anchor','title','address','maps_url')):
            entry['google_conflict'] = finding
        return
