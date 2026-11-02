# Private problem-report email delivery

This Worker receives the map’s “Report a problem” form, verifies Cloudflare
Turnstile, and delivers the report with Resend. No email address or secret is
included in the static site.

## One-time setup

The public HTML currently uses
`https://bassbeermap.bristolbagpiper.workers.dev/api/report-problem`.
`wrangler.toml` contains a starter name, `bassbeermap-report`; it is not proof
of the live Worker name or revision. For an existing deployment, first confirm
its name, account and routes in Cloudflare and target that Worker explicitly.
No repository workflow deploys the Worker. The setup below is for provisioning
a Worker, not a map-data update or a report-delivery test.

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
The public site calls the Worker directly at its `workers.dev` URL. That URL
contains no secret. Browser cross-origin access is restricted to
`https://bassbeermap.com`; Origin is not authentication and requests without
Origin still require a valid Turnstile token. A separately authorized report
test must confirm Resend acceptance and receipt at `REPORT_TO`; a successful
config GET alone does not establish delivery.
