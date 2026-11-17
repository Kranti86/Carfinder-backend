# Security & payments fixes (Nov 2026)

## Deploy checklist
1. Set Heroku config vars **before** deploying:
   - `SESSION_SECRET` — long random string. **The app now refuses to boot on Heroku without it.**
   - `PAYPAL_WEBHOOK_ID` — PayPal webhooks are now **rejected** unless they can be verified.
   - Optional: `UPSTREAM_DAILY_CAP` (max paid Booking API calls/day), `METRICS_TOKEN`.
2. Deploy this backend, then deploy the matching frontend **straight after** — the new
   checkout fields (`job_id`, `vehicle_id`, `insurance`) are required; an old frontend
   gets `409 quote_expired` on `/requests`.
3. The new `reservations.paypal_order_id` column is added automatically on boot.

## What changed
- **Server-side pricing.** `app/services/quotes.py` snapshots each search's prices.
  `/quote`, `/paypal/create-order` and `/requests` compute fee + insurance on the
  server; client-sent amounts are ignored. PayPal orders are bound to their quote and
  reservation id, the held amount is checked (mismatch → voided), and `/requests` is
  idempotent per PayPal order.
- **`/requests` and `/paypal/create-order`**: origin guard + per-client rate limits,
  customer validation.
- **`/lookup`**: full 8-char reference + exact email or last name; no wildcards; rate-limited.
- **Portal Cancel** voids an active hold; `capture-due` skips cancelled bookings; Refund/Void
  only act on the right payment state.
- **Webhooks**: unverifiable events rejected; reservations matched via custom_id, order id,
  related ids and the "up" link (refund → capture).
- **Credit protection**: `/vehicle`, `/supplier`, `/booking-summary` guarded + rate-limited;
  optional daily cap on all paid upstream calls (`app/budget.py`); `/stats?cache_only=true`.
- **Ops**: `/metrics` needs a token (hidden if none set); `/docs` off unless `ENABLE_DOCS=true`.
- Portal list counts use one GROUP BY query. `greenlet` pinned. Docs updated.
- Tests: `tests/test_checkout.py` (16 new; 69 total passing).

## Customer Match: all identifiers + value segments
- Reservations now store `billing_zip` / `billing_country`, taken from PayPal's order
  (card billing address, or the PayPal payer/shipping address) when the hold is placed.
- Audience CSVs use Google's full hashed format: Email, Phone, First Name, Last Name
  (SHA-256) + Country, Zip (clear). Address columns are filled only when all four parts exist.
- New segments: `high-value` (top 20% by lifetime booking value; optional
  `HIGH_VALUE_MIN_USD` floor), `recent-customers` (90 days), `lapsed-customers`
  (180+ days, win-back), `unconverted` (reserved but never paid).

## Slack: instant alerts + daily digest
- `app/services/notify.py`: one place for Slack. New-booking alert now says whether the
  hold was approved, declined (with the phone to call), in review, or unpaid.
- Alerts for: PayPal approving a held-for-review card, hold voided/expired, capture
  (manual or PayPal), capture denied/failed, refunds/chargebacks, disputes, cancellations,
  failed hold releases, final confirmation sent, fulfilled, and each auto-capture run.
  PayPal echoes of actions we already alerted on are not repeated.
- `python -m app.cli daily` = auto-capture + daily digest (run by the cron): last-24h
  bookings and money, untouched bookings (no agent action yet), pickups within 48h without
  a final confirmation, declined cards to call back, holds stuck in review or past the
  capture window, disputes, and open/awaiting-confirmation counts. Test bookings excluded.
- The Slack webhook URL is scrubbed from httpx request logs.
- Env: `SLACK_WEBHOOK_URL` (web + cron), optional `ADMIN_BASE_URL` for links.

## Audit fixes
- Google Ads conversion uploads now skip bookings whose visitor clicked "Reject" on the
  cookie banner (`ads_opt_out`), matching the audience exports and the cookie policy.
- Admin login is rate-limited (10 attempts / 15 min per client; `LOGIN_RATE_LIMIT`,
  `LOGIN_RATE_WINDOW`) to stop password guessing.

## Run it from Slack
- `/rentals` slash command: today, untouched, open, find, `<REF>`, capture-due, users,
  add-user, reset-password, disable-user, enable-user, whoami.
- Buttons on every booking alert and card: Claim, Capture, Send confirmation (form with
  supplier ref + PDF upload, emails the customer), Mark fulfilled, Refund, Cancel (form with
  reason), Add note, Open in admin. Money buttons ask for confirmation with the amount.
- Only Slack users listed in `SLACK_ADMINS` (`U…:admin-login-email`) can see or act;
  actions are logged under that admin login. Every request is verified with
  `SLACK_SIGNING_SECRET` (with replay protection). `SLACK_BOT_TOKEN` opens forms and
  downloads attached PDFs (scopes: commands, files:read).
- Booking actions moved to `app/services/actions.py`, shared by /admin and Slack.
- Fix: attaching a confirmation PDF in /admin no longer errors after saving.

## Super-admin delete
- `SUPER_ADMINS` (comma-separated emails, set on Railway) defines super admins. It can't be
  granted from inside the app.
- Booking page: "Delete reservation (super admin)" — type the ref to confirm. Refused while a
  card hold is active (cancel first). Captured bookings warn that refunds must then be done
  in PayPal. Posts a Slack notice.
- Slack: `/rentals delete <REF>` → confirm button. Super admins only.
- The delete-all page (/admin/purge) is now super-admin only as well.

## SEO location pages
- Fix: "from $X/day" divided every search by 3 days; it now uses the real trip length
  (a 7-day search was showing ~2.3× too high on the booking page tiles).
- Every real search saves its place's price facts to Postgres (`city_stats`) plus a daily
  history (`city_stat_days`) — no extra API calls. Searches within 4 km merge into one
  place; top-30 US airports get their own airport pages.
- `seo-seed`: refreshes the stalest airports, `SEO_SEED_PER_DAY` (default 5) per day, each
  through the normal daily credit cap. Runs inside `python -m app.cli daily`.
- Pages (served on the main domain via the frontend proxy): `/car-rental`,
  `/car-rental/{slug}`, `/car-rental/sitemap.xml`, `/car-rental/popular.json`.
  Published only with ≥10 cars and fresh (<45 days) prices; cities need ≥3 real searches.
  FAQ + breadcrumb structured data; canonical on the main domain; noindex when opened on
  the Railway domain directly.
- `/stats` falls back to the saved facts when the 24h cache copy has expired.

## Security hardening (Nov 2026 review)
- **Rate limits could be bypassed** by sending a new `X-Session-Id` on every request
  (confirmed live). Limits are now keyed on the client IP only.
- `/canary` (a paid upstream call) is hidden unless `CANARY_TOKEN` is set, and counts
  against the daily search cap.
- Confirmation emails go out only for a held card (or in-review hold), or when nothing is
  owed online — the booking form can no longer be used to email strangers.
- Two-factor sign-in for /admin (authenticator app, QR setup at /admin/security, replay-proof
  codes). Super admins can reset someone's two-factor from Slack: `/rentals reset-2fa <email>`.
- Slack security alerts: every admin sign-in (flags logins without two-factor), password-
  guessing lockouts, search budget at 80%/100%, 3+ card declines in an hour.
- Bot check (Cloudflare Turnstile) on search, checkout and unpaid bookings — off until
  `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY` are set. One pass covers 30 minutes per IP.
- Security headers: HSTS everywhere; admin/Slack pages can't be framed and aren't cached.
- Libraries upgraded: FastAPI 0.163.1 (Starlette 1.10), python-multipart 0.0.34,
  jinja2 3.1.6, python-dotenv 1.2.2, pytest 9.0.3. pip-audit: no known vulnerabilities.
