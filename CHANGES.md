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
