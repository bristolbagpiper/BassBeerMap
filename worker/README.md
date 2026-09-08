# Private problem-report email delivery

This Worker receives the map’s “Report a problem” form, verifies Cloudflare
Turnstile, and delivers the report with Resend. No email address or secret is
included in the static site.

## One-time setup

1. Add `bassbeermap.com` to Cloudflare and proxy the DNS record that points to
   GitHub Pages.
2. In Cloudflare Turnstile, create a widget for `bassbeermap.com`.
3. In Resend, verify a sender domain/address and create an API key.
4. From this `worker` folder, authenticate and create the Worker:

   ```sh
   npx wrangler login
   npx wrangler secret put RESEND_API_KEY
   npx wrangler secret put REPORT_TO
   npx wrangler secret put REPORT_FROM
   npx wrangler secret put TURNSTILE_SECRET_KEY
   npx wrangler secret put TURNSTILE_SITE_KEY
   npx wrangler secret put ALLOWED_ORIGIN
   npx wrangler deploy
   ```

   `ALLOWED_ORIGIN` must be exactly `https://bassbeermap.com`. `REPORT_FROM`
   must be a verified Resend sender, e.g. `Bass Map <reports@bassbeermap.com>`.
5. In the Cloudflare Worker dashboard, add `reports.bassbeermap.com` as a
   **Custom Domain** for this Worker. Cloudflare creates the required DNS
   record automatically.

The public site calls `https://reports.bassbeermap.com/api/report-problem`.
Test a report after setup and confirm it reaches `REPORT_TO`.
