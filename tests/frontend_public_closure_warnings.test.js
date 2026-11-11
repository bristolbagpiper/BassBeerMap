const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const inlineScript = html.match(/<script>\s*([\s\S]*?)<\/script>/i)?.[1];
assert.ok(inlineScript, 'inline frontend script exists');
assert.doesNotThrow(() => new vm.Script(inlineScript), 'inline frontend script parses');
const start = html.indexOf('    function isCurrentPublicClosureEvidence(');
const end = html.indexOf('    async function loadPossibleClosures()', start);
assert.notEqual(start, -1, 'public closure evidence validator exists');
assert.notEqual(end, -1, 'public closure evidence validator has a bounded source region');
const source = html.slice(start, end);
const context = vm.createContext({});
vm.runInContext(`
  const MAX_PUBLIC_CLOSURE_AGE_MS = 30 * 24 * 60 * 60 * 1000;
  ${source}
  globalThis.contract = { isCurrentPublicClosureEvidence, closureEvidenceMatchesVenue };
`, context);

const contract = context.contract;
const now = Date.parse('2026-11-11T12:00:00.000Z');
const finding = {
  venue_id: 'v-example',
  pub_name: 'Example Inn',
  place_name: 'Exampleton',
  postcode: 'EX1 2AB',
  status: 'review_needed',
  binding_signature: 'a'.repeat(64),
  business_status: 'CLOSED_TEMPORARILY',
  checked_at: '2026-11-10T12:00:00.000Z',
  current_pin: {lat: 51.5, lng: -0.1},
};
const item = {
  locationPrecision: 'venue',
  venue_id: finding.venue_id,
  pub_name: finding.pub_name,
  place_name: finding.place_name,
  postcode: finding.postcode,
  lat: 51.5,
  lng: -0.1,
};

test('fresh explicitly closed evidence with exact identity and point is eligible', () => {
  assert.equal(contract.isCurrentPublicClosureEvidence(finding, now), true);
  assert.equal(contract.closureEvidenceMatchesVenue(finding, item), true);
});

test('stale, future, or undated closure evidence fails closed', () => {
  assert.equal(contract.isCurrentPublicClosureEvidence({...finding, checked_at: '2026-10-11T11:59:59.999Z'}, now), false);
  assert.equal(contract.isCurrentPublicClosureEvidence({...finding, checked_at: '2026-11-11T12:01:00.000Z'}, now), false);
  const {checked_at, ...legacy} = finding;
  assert.equal(contract.isCurrentPublicClosureEvidence(legacy, now), false);
});

test('ambiguous status and missing binding evidence do not produce closure warnings', () => {
  assert.equal(contract.isCurrentPublicClosureEvidence({...finding, business_status: 'OPERATIONAL'}, now), false);
  assert.equal(contract.isCurrentPublicClosureEvidence({...finding, current_pin: undefined}, now), false);
  assert.equal(contract.isCurrentPublicClosureEvidence({...finding, binding_signature: 'old'}, now), false);
});

test('identity and current map point must match exactly', () => {
  assert.equal(contract.closureEvidenceMatchesVenue(finding, {...item, pub_name: 'Different Inn'}), false);
  assert.equal(contract.closureEvidenceMatchesVenue(finding, {...item, postcode: 'EX1 9ZZ'}), false);
  assert.equal(contract.closureEvidenceMatchesVenue(finding, {...item, lng: -0.100001}), false);
});
