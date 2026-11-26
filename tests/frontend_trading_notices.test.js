const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const inlineScript = html.match(/<script>\s*([\s\S]*?)<\/script>/i)?.[1];
assert.ok(inlineScript, 'inline frontend script exists');
assert.doesNotThrow(() => new vm.Script(inlineScript), 'inline frontend script parses');
const start = html.indexOf('    function noticeDate(');
const end = html.indexOf('    function noticeMarkup(', start);
assert.ok(start >= 0 && end > start, 'public trading notice functions exist');
const entries = new Map();
const hidden = new Set();
const context = vm.createContext({tradingNoticeEntries: entries, hiddenTradingNoticeVenueIds: hidden});
vm.runInContext(html.slice(start, end), context);

const venue = {venue_id: 'v-example'};
const fact = {venue_id: venue.venue_id, text: 'Operator reported a temporary closure.',
  published_at: '2026-11-15', review_by: '2026-11-20'};

function show(notice, instant = '2026-11-20T12:00:00Z') {
  entries.set(venue.venue_id, notice);
  return context.tradingNotice(venue, Date.parse(instant));
}

test('shows a dated notice through its review date, then asks visitors to check', () => {
  assert.equal(show(fact, '2026-11-20T23:59:59Z'), fact.text);
  assert.equal(show(fact, '2026-11-21T00:00:00Z'),
    'This trading status notice is due for review. Check before travelling.');
});

test('hides a future, undated or malformed statement', () => {
  for (const date of [undefined, null, '2026-02-30', '2026-11-21', '20261115']) {
    assert.equal(show({...fact, published_at: date}), '');
  }
  for (const date of [null, '', '2026-11-14', '2026-02-30']) {
    assert.equal(show({...fact, review_by: date}), '');
  }
});

test('binds the statement to the listed venue and respects manual hides', () => {
  assert.equal(show({...fact, venue_id: 'v-different'}), '');
  hidden.add(venue.venue_id);
  try { assert.equal(show(fact), ''); } finally { hidden.delete(venue.venue_id); }
});

test('allows a dated historical fact with no review deadline', () => {
  const {review_by, ...historic} = fact;
  assert.equal(show(historic, '2027-01-01T00:00:00Z'), fact.text);
});
