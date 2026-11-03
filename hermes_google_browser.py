"""Bounded, no-Google-API Google Maps observations through Hermes native browser tools.

This records private comparison receipts only. It does not approve or change pins.
The caller must provide an isolated HERMES_HOME configured for the local/built-in
browser and with real-profile use disabled. No browser is started at import time.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from build_place_review_queue import build_queue, valid_google_result_url
from coordinate_sources import name_matches
from directory_release import active_evidence, distance_metres, point_valid, read_json
from owner_location_review import primary_title

MAX_BATCH = 45
PAGE_DEADLINE = 20
TOTAL_DEADLINE = 20 * 60
CATEGORIES = {'identity_and_pin_consistent', 'identity_conflict', 'pin_conflict',
              'trading_conflict', 'ambiguous', 'unavailable'}
OBSERVATION_FIELDS = ('schema_version', 'provider', 'venue_id', 'identity_signature',
                      'point_signature', 'binding_signature', 'category', 'attempted_at',
                      'checked_at', 'result_url', 'source_url')
_DOM_READ = r"""JSON.stringify((()=>{const vis=e=>!!(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none';const text=e=>(e.innerText||e.getAttribute('aria-label')||'').trim();const inView=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth};return {url:location.href,title:document.title,h1:[...document.querySelectorAll('h1')].filter(vis).map(text),addressButtons:[...document.querySelectorAll('button')].filter(vis).map(e=>({label:(e.getAttribute('aria-label')||'').trim(),text:text(e)})).filter(x=>/^address\s*:/i.test(x.label)),consentButtons:[...document.querySelectorAll('button')].filter(vis).map(e=>({label:(e.getAttribute('aria-label')||'').trim(),text:text(e),inViewport:inView(e)})).filter(x=>/reject\s+all/i.test(x.label+' '+x.text)),placeLinks:[...document.querySelectorAll('a[href*="/maps/place/"]')].filter(vis).map(e=>({href:e.href,text:text(e)})).slice(0,30),body:(document.body?.innerText||'').slice(0,12000)}})())"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _response(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {'success': False, 'error': 'invalid native browser response'}


def _runtime_tools():
    home = os.environ.get('HERMES_HOME')
    if not home:
        raise RuntimeError('HERMES_HOME must point to a dedicated caller-configured Hermes profile')
    if os.environ.get('HERMES_BROWSER_REAL_PROFILE', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        raise RuntimeError('real-profile browser use is forbidden for this worker')
    runtime = Path(os.environ.get('HERMES_RUNTIME', Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'hermes' / 'hermes-agent'))
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    browser = importlib.import_module('tools.browser_tool')
    lifecycle = importlib.import_module('tools.browser_tool_lifecycle')
    cloud = getattr(browser, '_cloud', None)
    real_profile_check = getattr(cloud, '_use_real_profile', None)
    backend_name = getattr(cloud, 'browser_backend_name', None)
    local_mode_check = getattr(cloud, '_is_local_mode', None)
    if not all(callable(value) for value in (real_profile_check, backend_name, local_mode_check)):
        raise RuntimeError('installed Hermes browser runtime lacks the required backend/profile checks')
    if real_profile_check():
        raise RuntimeError('Hermes browser.use_real_profile must be false')
    if backend_name() not in ('local', 'lightpanda') or not local_mode_check():
        raise RuntimeError('Hermes browser must use its local built-in backend')
    if browser._is_camofox_mode() or browser._is_browser_use_cli_mode():
        raise RuntimeError('Camofox and Browser Use backends are outside this worker contract')
    if not callable(getattr(browser, 'browser_scroll', None)):
        raise RuntimeError('installed Hermes browser runtime lacks native browser_scroll')
    return browser, lifecycle.cleanup_browser


def _postcode(value):
    return re.sub(r'\s+', '', str(value or '')).upper()


def _postcode_in_text(compact_postcode, text):
    if not compact_postcode:
        return False
    pattern = r'(?<![A-Z0-9])' + r'\s*'.join(map(re.escape, compact_postcode)) + r'(?![A-Z0-9])'
    return bool(re.search(pattern, str(text or ''), re.I))


def _valid_place_url(value):
    return valid_google_result_url(str(value or ''), require_named_place=True)


def _point_from_url(value):
    matches = list(re.finditer(r'!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)', value))
    if len(matches) != 1:
        return None
    try:
        point = {'lat': float(matches[0].group(1)), 'lng': float(matches[0].group(2))}
        return point if point_valid(point) else None
    except ValueError:
        return None


def _reference_expectations(item, registry_entry):
    pin = registry_entry.get('verified_pin') or {}
    evidence = active_evidence(pin)
    names = {primary_title(e['reference_name']) for e in evidence if e.get('reference_name')}
    postcodes = {item.get('postcode', '')}
    postcodes.update(str(e.get('reference_postcode') or '').strip() for e in evidence)
    issue = pin.get('metadata_issue') or {}
    postcodes.add(str(issue.get('reference') or '').strip())
    return names, {_postcode(x) for x in postcodes if _postcode(x)}


def _identity_ok(item, title, ref_names):
    names = {item.get('pub_name', ''), *ref_names}
    return any(name and name_matches(name, title, item.get('place_name', '')) for name in names)


def _consent_ref(snapshot):
    """Return a native snapshot ref only for an ordinary visible consent rejection."""
    text = str(snapshot.get('snapshot') or snapshot.get('text') or '')
    if not any(word in text.lower() for word in ('before you continue', 'consent', 'cookies')):
        return None
    for line in text.splitlines():
        if re.search(r'\breject all\b', line, re.I):
            match = re.search(r'\[(?:ref=)?(e\d+)\]', line, re.I)
            if match:
                return match.group(1)
    return None


def _search_result_ref(snapshot_text, place_links, expected_name, place_name, ref_names):
    """Correlate one uniquely named visible place link with one native ref."""
    by_label = {}
    for link in place_links if isinstance(place_links, list) else []:
        if not isinstance(link, dict):
            continue
        label = re.sub(r'\s+', ' ', str(link.get('text') or '')).strip()
        href = str(link.get('href') or '')
        if label and _valid_place_url(href) and _identity_ok(
                {'pub_name': expected_name, 'place_name': place_name}, label, ref_names):
            by_label.setdefault(label.casefold(), {}).setdefault(href, label)
    candidates = {href: label for links in by_label.values() for href, label in links.items()}
    if len(candidates) != 1:
        return None
    href, label = next(iter(candidates.items()))
    # Snapshot refs do not expose hrefs. Only correlate by exact observable label
    # when that label itself identifies one visible DOM URL.
    labels_for_href = [key for key, links in by_label.items() if href in links]
    if len(labels_for_href) != 1:
        return None
    refs = []
    for line in str(snapshot_text or '').splitlines():
        match = re.search(r'\blink\s+["\u201c](.*?)["\u201d]\s+\[(?:ref=)?(e\d+)\]', line, re.I)
        if match and re.sub(r'\s+', ' ', match.group(1)).strip().casefold() == label.casefold():
            refs.append(match.group(2))
    return refs[0] if len(refs) == 1 else None


def _extract(browser, task_id):
    result = _response(browser.browser_console(expression=_DOM_READ, task_id=task_id))
    if not result.get('success'):
        raise RuntimeError('native browser DOM read failed')
    data = result.get('result')
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            raise RuntimeError('native browser DOM read was not structured')
    if not isinstance(data, dict):
        raise RuntimeError('native browser DOM read was not structured')
    return data


def _resolve_page(browser, task_id, expected_name, expected_postcodes, ref_names, original_url='', place_name=''):
    deadline = time.monotonic() + PAGE_DEADLINE
    last = {}
    page_ready = False
    on_consent = False
    consent_attempted = False
    consent_scrolls = 0
    consent_wait_deadline = None
    followed_result = False
    while time.monotonic() < deadline:
        snap = _response(browser.browser_snapshot(full=False, task_id=task_id))
        if not snap.get('success'):
            raise RuntimeError('native browser snapshot failed')
        snap_text = str(snap.get('snapshot') or snap.get('text') or '')
        on_consent = ('consent.google.com' in str(snap.get('url') or '').lower()
                      or bool(re.search(r'before you continue|choose your privacy settings', snap_text, re.I)))
        if on_consent and consent_attempted:
            if consent_wait_deadline is None or time.monotonic() >= consent_wait_deadline:
                return {'category': 'unavailable', 'stop': True}
            time.sleep(min(1, max(0, consent_wait_deadline - time.monotonic())))
            continue
        if re.search(r'captcha|unusual traffic|automated quer|not a robot|sorry,? we can.t process', snap_text, re.I):
            return {'category': 'unavailable', 'stop': True}
        if on_consent and not consent_attempted:
            dom = _extract(browser, task_id)
            visible = any(x.get('inViewport') is True for x in dom.get('consentButtons', [])
                          if isinstance(x, dict))
            if not visible:
                if consent_scrolls >= 3:
                    return {'category': 'unavailable', 'stop': True}
                scroll = _response(browser.browser_scroll('down', task_id=task_id))
                if not scroll.get('success'):
                    raise RuntimeError('native consent scroll failed')
                consent_scrolls += 1
                time.sleep(min(.25, max(0, deadline - time.monotonic())))
                continue
            # Scroll may change ref IDs; take a fresh compact snapshot immediately
            # before the one native click.
            fresh = _response(browser.browser_snapshot(full=False, task_id=task_id))
            if not fresh.get('success'):
                raise RuntimeError('native consent refresh snapshot failed')
            fresh_ref = _consent_ref(fresh)
            if not fresh_ref:
                continue
            click = _response(browser.browser_click(fresh_ref, task_id=task_id))
            if not click.get('success'):
                raise RuntimeError('native consent click failed')
            consent_attempted = True
            consent_wait_deadline = min(deadline, time.monotonic() + 8)
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            continue
        last = _extract(browser, task_id)
        url = str(last.get('url') or '')
        # Results is a normal Maps search heading, not a challenge. Follow a
        # single named visible candidate only through its correlated native ref;
        # the detail page must still pass the full identity/postcode checks below.
        is_search = '/maps/search' in urlparse(url).path
        is_results_heading = (isinstance(last.get('h1'), list)
                              and len(last['h1']) == 1
                              and str(last['h1'][0]).strip().casefold() == 'results')
        if is_search and (not last.get('h1') or is_results_heading):
            if not followed_result:
                ref = _search_result_ref(snap_text, last.get('placeLinks'),
                                         expected_name, place_name, ref_names)
                if ref:
                    click = _response(browser.browser_click(ref, task_id=task_id))
                    if not click.get('success'):
                        raise RuntimeError('native result click failed')
                    followed_result = True
                    continue
            urls = [str(x.get('href', '')) for x in last.get('placeLinks', [])
                    if isinstance(x, dict) and _valid_place_url(x.get('href'))]
            return {'category': 'ambiguous', 'result_url': original_url or url, 'stop': False}
        if (isinstance(last.get('h1'), list) and len(last['h1']) == 1
                and isinstance(last.get('addressButtons'), list) and len(last['addressButtons']) == 1
                and _valid_place_url(url)):
            page_ready = True
            break
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    if on_consent and not page_ready:
        return {'category': 'unavailable', 'stop': True}
    if not last:
        return {'category': 'unavailable', 'stop': False}
    url = str(last.get('url') or '')
    titles = last.get('h1') if isinstance(last.get('h1'), list) else []
    addresses = last.get('addressButtons') if isinstance(last.get('addressButtons'), list) else []
    if len(titles) != 1 or len(addresses) != 1 or not _valid_place_url(url):
        if _valid_place_url(url):
            return {'category': 'ambiguous', 'result_url': url, 'stop': False}
        visible_places = [str(x.get('href', '')) for x in last.get('placeLinks', [])
                          if isinstance(x, dict) and _valid_place_url(x.get('href'))]
        if visible_places:
            return {'category': 'ambiguous', 'result_url': visible_places[0], 'stop': False}
        return {'category': 'unavailable', 'stop': False}
    title, address = str(titles[0]), str(addresses[0].get('label') or addresses[0].get('text') or '')
    postcode_ok = any(pc in _postcode(address) for pc in expected_postcodes)
    identity_ok = _identity_ok({'pub_name': expected_name, 'place_name': place_name}, title, ref_names)
    if not identity_ok or not postcode_ok:
        return {'category': 'identity_conflict', 'result_url': url, 'stop': False}
    point = _point_from_url(url)
    if point is None:
        return {'category': 'ambiguous', 'result_url': url, 'stop': False}
    return {'category': 'identity_and_pin_consistent', 'result_url': url,
            'point': point, 'address': address, 'body': str(last.get('body') or ''), 'stop': False}


def classify(item, registry_entry, page):
    """Classify a sanitized DOM observation against directory and retained references."""
    category = page['category']
    if category != 'identity_and_pin_consistent':
        return [(category, page.get('result_url', ''))]
    if item.get('precision') != 'venue' or item.get('current_pin_source') == 'postcode_fallback':
        return [('ambiguous', page['result_url'])]
    out = []
    if distance_metres(item['current_pin'], page['point']) > 50:
        out.append(('pin_conflict', page['result_url']))
    body = page.get('body', '').casefold()
    if any(label in body for label in ('permanently closed', 'temporarily closed', 'closed permanently')):
        out.append(('trading_conflict', page['result_url']))
    if not out:
        out.append(('identity_and_pin_consistent', page['result_url']))
    return out


def _record(item, category, attempted_at, checked_at, result_url, search_url):
    return dict(schema_version=1, provider='hermes-browser', venue_id=item['venue_id'],
                identity_signature=item['identity_signature'], point_signature=item['point_signature'],
                binding_signature=item['binding_signature'], category=category,
                attempted_at=attempted_at, checked_at=checked_at,
                result_url=result_url if category != 'unavailable' else '',
                source_url='' if category != 'unavailable' else search_url)


def _validate_prior(root, document):
    from hermes_google_review import build_status
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'observations'}
            or type(document.get('schema_version')) is not int or document['schema_version'] != 1
            or not isinstance(document.get('observations'), list)):
        raise ValueError('existing receipt document has an invalid schema')
    build_status(root, document)
    return document


def _rotated(queue, prior, limit):
    """Oldest exact-bound attempts first; preserve risk order for timestamp ties."""
    attempted = {}
    current = {item['venue_id']: item for item in queue}
    for observation in prior['observations']:
        item = current.get(observation.get('venue_id'))
        if item and all(observation.get(field) == item[field] for field in
                        ('identity_signature', 'point_signature', 'binding_signature')):
            value = datetime.fromisoformat(observation['attempted_at'].replace('Z', '+00:00')).astimezone(timezone.utc)
            previous = attempted.get(item['venue_id'])
            if previous is None or value > previous:
                attempted[item['venue_id']] = value
    return [item for _, item in sorted(enumerate(queue),
                                       key=lambda pair: (attempted.get(pair[1]['venue_id'], datetime.min.replace(tzinfo=timezone.utc)), pair[0]))[:limit]]


def run(root, receipts_path, limit=45, venue_ids=(), debug=False, browser=None, cleanup=None,
        clock=utc_now, sleeper=time.sleep):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f'limit must be between 1 and {MAX_BATCH}')
    root, receipts_path = Path(root), Path(receipts_path)
    lock_path = receipts_path.with_name(receipts_path.name + '.lock')
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError('another browser receipt writer holds the local lock') from exc
    task_id = 'hermes-google-' + uuid.uuid4().hex
    records = []
    stop = False
    temporary = None
    started = time.monotonic()
    try:
        os.write(lock_fd, task_id.encode())
        os.close(lock_fd)
        bundle = read_json(root / 'directory-release.json')
        registry = read_json(root / 'venue-registry.json')
        queue = build_queue(bundle, registry, read_json(root / 'audit/full-location-audit.json', {}))
        prior = _validate_prior(root, read_json(receipts_path, {'schema_version': 1, 'observations': []}))
        by_id = {x['venue_id']: x for x in queue}
        selected = [by_id[x] for x in venue_ids if x in by_id] if venue_ids else _rotated(queue, prior, limit)
        if venue_ids:
            if len(venue_ids) != len(set(venue_ids)) or len(selected) != len(venue_ids) or len(selected) > limit:
                raise ValueError('selected venue IDs must be unique, active, and within limit')
        if len(selected) > MAX_BATCH:
            raise ValueError('batch exceeds hard maximum')
        if browser is None:
            browser, cleanup = _runtime_tools()
        for item in selected:
            if time.monotonic() - started >= TOTAL_DEADLINE:
                break
            search_url = item['google_named_search_url']
            attempted = clock()
            try:
                nav = _response(browser.browser_navigate(search_url, task_id=task_id))
                if not nav.get('success') or nav.get('bot_detection_warning'):
                    page = {'category': 'unavailable', 'stop': True}
                else:
                    names, postcodes = _reference_expectations(item, registry['venues'][item['venue_id']])
                    page = _resolve_page(browser, task_id, item['pub_name'], postcodes, names, search_url,
                                         place_name=item.get('place_name', ''))
                if page['category'] == 'identity_and_pin_consistent':
                    cats = classify(item, registry['venues'][item['venue_id']], page)
                    checked = clock()
                    for category, result_url in cats:
                        records.append(_record(item, category, attempted, checked, result_url, search_url))
                    debug_categories = [category for category, _ in cats]
                else:
                    checked = None if page['category'] == 'unavailable' else clock()
                    records.append(_record(item, page['category'], attempted, checked,
                                           page.get('result_url', ''), search_url))
                    debug_categories = [page['category']]
                if debug:
                    print(json.dumps({'venue_id': item['venue_id'], 'categories': debug_categories}))
                if page.get('stop'):
                    stop = True
                    break
            except Exception as exc:
                records.append(_record(item, 'unavailable', attempted, None, '', search_url))
                if debug:
                    print(json.dumps({'venue_id': item['venue_id'], 'category': 'unavailable',
                                      'reason': type(exc).__name__}))
                stop = True
                break
            if time.monotonic() - started >= TOTAL_DEADLINE:
                break
        if len({record['venue_id'] for record in records}) != len(selected):
            stop = True  # A batch deadline is incomplete coverage, not success.
        if any(set(x) != set(OBSERVATION_FIELDS) for x in records):
            raise ValueError('internal receipt schema mismatch')
        payload = json.dumps({'schema_version': 1, 'observations': prior['observations'] + records},
                             ensure_ascii=False, indent=2) + '\n'
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='\n',
                                         dir=receipts_path.parent, prefix=receipts_path.name + '.',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, receipts_path)
    finally:
        try:
            if cleanup:
                cleanup(task_id)
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
    return {'attempted': len({record['venue_id'] for record in records}), 'stopped': stop}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=45)
    parser.add_argument('--venue-id', action='append', default=[])
    parser.add_argument('--debug', action='store_true', help='print sanitized per-venue status only')
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.receipts, args.limit, args.venue_id, args.debug)))


if __name__ == '__main__':
    main()
