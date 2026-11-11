const DEFAULT_ALLOWED_ORIGIN = 'https://bassbeermap.com';
const MAX_REPORT_BODY_BYTES = 32 * 1024;

function jsonHeaders(allowedOrigin) {
  return {
    'content-type': 'application/json; charset=utf-8',
    'access-control-allow-origin': allowedOrigin,
    'access-control-allow-methods': 'GET, POST, OPTIONS',
    'access-control-allow-headers': 'content-type'
  };
}

function response(body, status = 200, headers) {
  return new Response(JSON.stringify(body), { status, headers });
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[character]);
}

function validEmail(value) {
  return !value || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

async function readJsonBody(request) {
  const contentLength = request.headers.get('Content-Length');
  if (contentLength !== null) {
    if (!/^\d+$/.test(contentLength)) return { error: 'invalid' };
    if (Number(contentLength) > MAX_REPORT_BODY_BYTES) return { error: 'too_large' };
  }
  if (!request.body) return { error: 'invalid' };

  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_REPORT_BODY_BYTES) {
        await reader.cancel();
        return { error: 'too_large' };
      }
      chunks.push(value);
    }
  } catch {
    return { error: 'invalid' };
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return { value: JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes)) };
  } catch {
    return { error: 'invalid' };
  }
}

async function verifyTurnstile(token, request, env) {
  const body = new FormData();
  body.set('secret', env.TURNSTILE_SECRET_KEY);
  body.set('response', token);
  body.set('remoteip', request.headers.get('CF-Connecting-IP') || '');
  try {
    const result = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', { method: 'POST', body });
    if (!result.ok) return { available: false, valid: false };
    const verification = await result.json();
    return { available: true, valid: verification?.success === true };
  } catch {
    return { available: false, valid: false };
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const allowedOrigin = env.ALLOWED_ORIGIN || DEFAULT_ALLOWED_ORIGIN;
    const headers = jsonHeaders(allowedOrigin);
    const origin = request.headers.get('Origin');
    if (origin && origin !== allowedOrigin) return response({ error: 'Invalid origin.' }, 403, headers);
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers });

    if (request.method === 'GET' && url.pathname === '/api/report-problem' && url.searchParams.has('config')) {
      return response({ turnstileSiteKey: env.TURNSTILE_SITE_KEY || '' }, 200, headers);
    }
    if (request.method !== 'POST' || url.pathname !== '/api/report-problem') {
      return response({ error: 'Not found.' }, 404, headers);
    }

    const parsed = await readJsonBody(request);
    if (parsed.error === 'too_large') return response({ error: 'Report is too large.' }, 413, headers);
    if (parsed.error || !parsed.value || typeof parsed.value !== 'object' || Array.isArray(parsed.value)) {
      return response({ error: 'Invalid report.' }, 400, headers);
    }
    const report = parsed.value;
    const venue = String(report.venue || '').trim().slice(0, 160);
    const message = String(report.message || '').trim().slice(0, 4000);
    const contact = String(report.contact || '').trim().slice(0, 254);
    const page = String(report.page || '').trim().slice(0, 500);
    if (report.website || !message) return response({ error: 'Report could not be accepted.' }, 400, headers);
    if (!validEmail(contact)) return response({ error: 'Enter a valid reply email or leave it blank.' }, 400, headers);
    if (!report.turnstileToken) return response({ error: 'Spam-protection check failed. Please try again.' }, 400, headers);
    if (!env.TURNSTILE_SECRET_KEY) {
      return response({ error: 'Spam-protection service is temporarily unavailable.' }, 502, headers);
    }

    const turnstile = await verifyTurnstile(report.turnstileToken, request, env);
    if (!turnstile.available) {
      return response({ error: 'Spam-protection service is temporarily unavailable.' }, 502, headers);
    }
    if (!turnstile.valid) return response({ error: 'Spam-protection check failed. Please try again.' }, 400, headers);
    if (!env.RESEND_API_KEY || !env.REPORT_FROM || !env.REPORT_TO) {
      return response({ error: 'Email delivery is temporarily unavailable. Please try again later.' }, 502, headers);
    }

    let email;
    try {
      email = await fetch('https://api.resend.com/emails', {
        method: 'POST',
        headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          from: env.REPORT_FROM,
          to: [env.REPORT_TO],
          reply_to: contact || undefined,
          subject: `Bass map problem report${venue ? `: ${venue}` : ''}`,
          html: `<h2>Bass map problem report</h2><p><strong>Venue/page:</strong> ${escapeHtml(venue || 'Not supplied')}</p><p><strong>Page:</strong> ${escapeHtml(page || 'Not supplied')}</p><p><strong>Reply email:</strong> ${escapeHtml(contact || 'Not supplied')}</p><p><strong>Report:</strong></p><p>${escapeHtml(message).replace(/\n/g, '<br>')}</p>`
        })
      });
    } catch {
      console.error('Resend delivery request failed');
      return response({ error: 'Email delivery is temporarily unavailable. Please try again later.' }, 502, headers);
    }
    if (!email.ok) {
      console.error('Resend rejected report with status', email.status);
      return response({ error: 'Email delivery is temporarily unavailable. Please try again later.' }, 502, headers);
    }
    return response({ ok: true }, 200, headers);
  }
};
