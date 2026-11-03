"""Bound human building checks to the exact listing and accepted coordinate.

A building confirmation does not verify postal delivery or current Bass stock.
It cannot follow a changed identity/point, or acknowledge a new source name.
"""
import math
import re
from datetime import date


IDENTITY_FIELDS = ('venue_id', 'pub_name', 'place_name', 'postcode', 'area', 'country')


def listing_snapshot(row):
    return {key: row.get(key, '') for key in IDENTITY_FIELDS}


def primary_title(value):
    return str(value or '').split(',')[0].split(' - ')[0].strip()


def source_identity_title(value):
    # Marketing after " - " is not an identity change. Preserve comma clauses:
    # they can identify a branch or premises, not just a town or SEO wording.
    return re.sub(r'\s+', ' ', str(value or '').split(' - ')[0]).strip().casefold()


def current_confirmation(row, pin, today=None):
    review = pin.get('owner_confirmation') or {}
    try:
        point = {key: pin[key] for key in ('lat', 'lng')}
        valid_point = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in point.values())
        checked = date.fromisoformat(review['checked_at'])
        if (type(review.get('schema_version')) is int and review['schema_version'] == 1
                and review.get('scope') == 'displayed_venue_building'
                and row.get('venue_id') and row.get('pub_name') and row.get('postcode')
                and review.get('listing') == listing_snapshot(row)
                and valid_point and review.get('approved_coordinate') == point
                and isinstance(review.get('reviewed_by'), str) and review['reviewed_by'].strip()
                and isinstance(review.get('owner_instruction'), str) and review['owner_instruction'].strip()
                and checked <= (today or date.today())):
            return review
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return None


def confirmation_fresh(row, pin, max_age_days=180, today=None):
    today = today or date.today()
    review = current_confirmation(row, pin, today)
    return bool(review and (today - date.fromisoformat(review['checked_at'])).days < max_age_days)


def pending_building_review(row, pin):
    request = pin.get('manual_review_request')
    if (not isinstance(request, dict) or type(request.get('schema_version')) is not int or request['schema_version'] != 1
            or request.get('scope') != 'building_coordinate' or not isinstance(request.get('id'), str) or not request['id']
            or request.get('venue_id') != row.get('venue_id')
            or request.get('questioned_coordinate') != {key: pin.get(key) for key in ('lat', 'lng')}
            or not isinstance(request.get('reason'), str) or not request['reason'] or not str(request.get('source_url', '')).startswith('https://')):
        return None
    confirmation = current_confirmation(row, pin)
    resolutions = confirmation.get('resolved_review_request_ids', []) if confirmation else []
    if isinstance(resolutions, list) and request['id'] in resolutions:
        return None
    return request


def source_name_acknowledged(row, pin, url, observed):
    review = current_confirmation(row, pin)
    if not review:
        return False
    normal = lambda value: re.sub(r'\s+', ' ', str(value or '')).strip().casefold()
    compact = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    snapshots = review.get('source_snapshots', [])
    if not isinstance(snapshots, list):
        return False
    for snapshot in snapshots:
        if (isinstance(snapshot, dict) and snapshot.get('url') == url
                and primary_title(snapshot.get('name')) and observed.get('name')
                and source_identity_title(snapshot.get('name')) == source_identity_title(observed.get('name'))
                and compact(snapshot.get('postcode')) == compact(observed.get('postcode'))
                and normal(snapshot.get('address')) == normal(observed.get('address'))):
            return True
    return False


def classify_review(record):
    """Label why attention is needed, without calling every warning a bad pin."""
    reasons = record.get('reasons', [])
    joined = ' '.join(reasons).casefold()
    if not record.get('active'):
        category = 'confirmed'
    elif 'trading status' in joined:
        category = 'trading_check'
    elif 'venue name changed' in joined or 'identity' in joined:
        category = 'identity_check'
    elif 'postcode' in joined and 'conflict' in joined:
        category = 'address_metadata'
    elif reasons and all('unavailable' in reason.casefold() for reason in reasons):
        category = 'source_warning'
    elif record.get('unverified'):
        category = 'needs_verification'
    elif 'manual location evidence' in joined or 'expired' in joined or 'not yet been' in joined or 'building comparison' in joined:
        category = 'needs_verification'
    else:
        category = 'location_check'
    actions = {
        'confirmed': 'No pin check requested',
        'source_warning': 'Source unavailable; retain the pin and retry the source, not the venue',
        'identity_check': 'Check whether the named business changed; this does not prove the coordinate is wrong',
        'trading_check': 'Check trading/Bass availability; do not move the pin from closure information alone',
        'address_metadata': 'Check the conflicting address/postcode; this does not prove the pin is wrong',
        'needs_verification': 'Corroborate missing or expired evidence; no incorrect pin has been established',
        'location_check': 'Check the named building or approximate/moved coordinate',
    }
    action = ('Compare the named building and saved pin; a source disagreement is not proof of an incorrect pin'
              if 'building comparison' in joined and category == 'needs_verification' else actions[category])
    return dict(review_category=category, manual_action_required=category not in ('confirmed', 'source_warning'), next_action=action)


def google_finding_assessment(finding):
    reason = finding.get('reason', '').casefold()
    trading = 'possible closure' in reason or 'trading status needs review' in reason
    location = 'approximate' in reason or 'differs by more than 50' in reason
    if trading and location:
        category, action = ('location_and_trading_check',
                            'Compare the named building and saved pin, and check trading status; disagreement is not proof of an incorrect pin')
    elif trading:
        category, action = 'trading_check', 'Check trading status, not the pin position'
    elif location:
        category, action = 'location_check', 'Verify the named building; disagreement is not proof of an incorrect pin'
    elif ('coordinates are missing' in reason or 'conclusive trading status' in reason
          or 'incomplete or ambiguous' in reason or 'did not identify one matching' in reason
          or 'returned no named place' in reason):
        category, action = 'source_warning', 'Source is inconclusive; no pin check requested'
    else:
        category, action = 'identity_check', 'Check the matched business identity and address; no incorrect coordinate has been established'
    return dict(review_category=category, manual_action_required=category != 'source_warning', next_action=action)
