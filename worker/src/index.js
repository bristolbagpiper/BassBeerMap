const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8' };

function response(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[character]);
}

function validEmail(value) {
  return !value || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

async function verifyTurnstile(token, request, env) {
  const body = new FormData();
  body.set('secret', env.TURNSTILE_SECRET_KEY);
  body.set('response', token);
  body.set('remoteip', request.headers.get('CF-Connecting-IP') || '');
  const result = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', { method: 'POST', body });
  return (await result.json()).success === true;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get('Origin');
    if (origin && origin !== env.ALLOWED_ORIGIN) return response({ error: 'Invalid origin.' }, 403);

    if (request.method === 'GET' && url.pathname === '/api/report-problem' && url.searchParams.has('config')) {
      return response({ turnstileSiteKey: env.TURNSTILE_SITE_KEY || '' });
    }
    if (request.method !== 'POST' || url.pathname !== '/api/report-problem') return response({ error: 'Not found.' }, 404);

    let report;
    try {
      report = await request.json();
    } catch {
      return response({ error: 'Invalid report.' }, 400);
    }
    const venue = String(report.venue || '').trim().slice(0, 160);
    const message = String(report.message || '').trim().slice(0, 4000);
    const contact = String(report.contact || '').trim().slice(0, 254);
    const page = String(report.page || '').trim().slice(0, 500);
    if (report.website || !message) return response({ error: 'Report could not be accepted.' }, 400);
    if (!validEmail(contact)) return response({ error: 'Enter a valid reply email or leave it blank.' }, 400);
    if (!report.turnstileToken || !(await verifyTurnstile(report.turnstileToken, request, env))) {
      return response({ error: 'Spam-protection check failed. Please try again.' }, 400);
    }

    const email = await fetch('https://api.resend.com/emails', {
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
    if (!email.ok) {
      console.error('Resend rejected report', await email.text());
      return response({ error: 'Email delivery is temporarily unavailable. Please try again later.' }, 502);
    }
    return response({ ok: true });
  }
};
