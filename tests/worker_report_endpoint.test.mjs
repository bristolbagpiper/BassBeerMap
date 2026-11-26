import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const {default: worker} = await import(pathToFileURL(resolve('worker/src/index.js')).href);
const originalFetch = globalThis.fetch;
const allowedOrigin = 'https://map.example';
const env = {
  ALLOWED_ORIGIN: allowedOrigin,
  TURNSTILE_SITE_KEY: 'public-site-key',
  TURNSTILE_SECRET_KEY: 'private-turnstile-secret',
  RESEND_API_KEY: 'private-resend-secret',
  REPORT_TO: 'reports@example.test',
  REPORT_FROM: 'map@example.test',
};

afterEach(() => { globalThis.fetch = originalFetch; });

function request(method, {origin = allowedOrigin, path = '/api/report-problem', body, headers = {}} = {}) {
  const requestHeaders = {...headers};
  if (origin !== null) requestHeaders.Origin = origin;
  if (body !== undefined && !requestHeaders['Content-Type']) requestHeaders['Content-Type'] = 'application/json';
  return new Request(`https://worker.test${path}`, {method, headers: requestHeaders, body});
}

test('preflight and all response paths use the configured allowed origin', async () => {
  const preflight = await worker.fetch(request('OPTIONS'), env);
  assert.equal(preflight.status, 204);
  assert.equal(preflight.headers.get('access-control-allow-origin'), allowedOrigin);

  const denied = await worker.fetch(request('OPTIONS', {origin: 'https://attacker.example'}), env);
  assert.equal(denied.status, 403);
  assert.equal(denied.headers.get('access-control-allow-origin'), allowedOrigin);

  const config = await worker.fetch(request('GET', {path: '/api/report-problem?config=1'}), env);
  assert.equal(config.status, 200);
  assert.equal(config.headers.get('access-control-allow-origin'), allowedOrigin);
  assert.deepEqual(await config.json(), {turnstileSiteKey: 'public-site-key'});
});

test('oversized body is rejected before provider calls', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new Error('provider must not be called'); };
  const body = JSON.stringify({message: 'x'.repeat(33 * 1024), turnstileToken: 'test'});
  const result = await worker.fetch(request('POST', {body}), env);
  assert.equal(result.status, 413);
  assert.equal(calls, 0);
});

test('Turnstile outage is reported as unavailable and never reaches Resend', async () => {
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(String(url));
    return new Response('upstream unavailable', {status: 503});
  };
  const body = JSON.stringify({message: 'Map issue', turnstileToken: 'test-token'});
  const result = await worker.fetch(request('POST', {body}), env);
  assert.equal(result.status, 502);
  assert.match((await result.json()).error, /temporarily unavailable/);
  assert.deepEqual(calls, ['https://challenges.cloudflare.com/turnstile/v0/siteverify']);
});

test('missing provider credentials fail before a report is sent', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return Response.json({success: true}); };
  const body = JSON.stringify({message: 'Map issue', turnstileToken: 'test-token'});
  const turnstileUnconfigured = await worker.fetch(request('POST', {body}), {...env, TURNSTILE_SECRET_KEY: ''});
  assert.equal(turnstileUnconfigured.status, 502);
  assert.equal(calls, 0);

  const resendUnconfigured = await worker.fetch(request('POST', {body}), {...env, RESEND_API_KEY: ''});
  assert.equal(resendUnconfigured.status, 502);
  assert.equal(calls, 1);
});

test('Resend outage returns a bounded error without exposing credentials', async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url: String(url), authorization: options.headers?.Authorization});
    if (String(url).includes('siteverify')) return Response.json({success: true});
    return new Response('private upstream diagnostic', {status: 503});
  };
  const body = JSON.stringify({message: 'Map issue', turnstileToken: 'test-token'});
  const result = await worker.fetch(request('POST', {body}), env);
  const responseBody = await result.text();
  assert.equal(result.status, 502);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, 'https://api.resend.com/emails');
  assert.equal(calls[1].authorization, 'Bearer private-resend-secret');
  assert.equal(responseBody.includes(env.RESEND_API_KEY), false);
  assert.equal(responseBody.includes('private upstream diagnostic'), false);
});

test('visitor feedback form payload is accepted through the endpoint with Resend mocked', async () => {
  const html = await readFile('index.html', 'utf8');
  for (const expected of ['id="problemReportForm"', 'name="venue"', 'name="message"',
    'name="contact"', 'name="website"', 'REPORT_PROBLEM_ENDPOINT']) {
    assert.ok(html.includes(expected), `visitor form missing ${expected}`);
  }
  const publicEnv = {...env, ALLOWED_ORIGIN: 'https://bassbeermap.com'};
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url: String(url), options});
    if (String(url).includes('/siteverify')) return Response.json({success: true});
    return Response.json({id: 'mock-resend-id'}, {status: 200});
  };
  const body = JSON.stringify({venue: 'The Example', message: '<script>alert("x")</script>\nWrong entrance',
    contact: 'visitor@example.test', website: '', turnstileToken: 'test-token',
    page: 'https://bassbeermap.com/#example'});
  const result = await worker.fetch(request('POST', {origin: 'https://bassbeermap.com', body}), publicEnv);
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), {ok: true});
  assert.deepEqual(calls.map(call => call.url), [
    'https://challenges.cloudflare.com/turnstile/v0/siteverify', 'https://api.resend.com/emails'
  ]);
  const email = JSON.parse(calls[1].options.body);
  assert.deepEqual(email.to, ['reports@example.test']);
  assert.equal(email.reply_to, 'visitor@example.test');
  assert.ok(email.html.includes('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;'));
  assert.equal(email.html.includes('<script>alert'), false);
  assert.equal(calls[1].options.headers.Authorization, 'Bearer private-resend-secret');
});
