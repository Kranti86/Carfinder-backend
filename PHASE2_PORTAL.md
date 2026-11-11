# Phase 2 — Reservation backend + Agent Ops Portal

What this adds to the carfinder backend, and how to switch it on.

## What's in it

- **Reservations database** (`app/db.py`, `app/models_db.py`): `reservations`,
  `agents`, `audit_log` tables. Uses Heroku Postgres via `DATABASE_URL`; falls back to
  local SQLite with no setup for dev/tests.
- **`POST /requests`** (`app/routers/requests.py`): persists every booking first, then
  places a PayPal **authorization hold** for the online amount (service fee +
  insurance) if a `paypal_order_id` is provided. Best-effort Slack ping.
  **All amounts are computed on the server** from the quote saved when the search
  finished (`app/services/quotes.py`); the browser only sends `job_id`, `vehicle_id`
  and whether insurance was chosen. Requests without a valid, unexpired quote get
  `409 quote_expired`, so any other caller (e.g. a phone-funnel tool) must send the
  same fields.
- **Ops portal** at **`/admin`** (`app/routers/admin.py` + `app/templates_admin/`):
  per-agent login, a deal-funnel reservation list (filter by status, search), and a
  detail page with **Capture / Refund / Void / Confirm / Mark fulfilled**, editable
  **supplier reservation number** and **policy number**, and free-text notes. Every
  action is written to the **audit trail with the acting agent's email**.
- **Payments** (`app/services/payments.py`): **PayPal** Orders v2 — authorize a hold,
  capture / void / refund. Runs in a safe "recorded, not charged" mode until you add
  credentials — so the whole flow works today and goes live the moment the keys are set,
  no code change.
- **Auto-capture after 3 days** (`app/cli.py capture-due`): run daily by Heroku Scheduler.
  (PayPal note: an authorization's guaranteed honor period is ~3 days — run the job
  at/under 3 days, or reauthorize for longer holds.)

## Switch-on steps

1. **Attach Postgres:** `heroku addons:create heroku-postgresql:essential-0 -a carfinder1`
   (sets `DATABASE_URL`). Tables are created automatically on boot.
2. **Set config vars** (Heroku → Settings → Config Vars):
   - `SESSION_SECRET` = a long random string (signs the login cookie). **Required.**
   - `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` (from your PayPal app),
     `PAYPAL_ENV` = `sandbox` or `production`.
   - `SLACK_WEBHOOK_URL` — optional, for a ping on each new reservation.
3. **Create your first agent** (after deploy):
   `heroku run "python -m app.cli create-agent you@company.com 'a-strong-password' 'Your Name' admin" -a carfinder1`
   Add more agents the same way (drop the `admin` for a normal agent).
4. **Schedule auto-capture:** add the **Heroku Scheduler** add-on and set a daily job:
   `python -m app.cli capture-due`
5. Visit **`https://carfinder1-….herokuapp.com/admin`** and sign in.

## PayPal webhooks (recommended for production)

The happy path is synchronous, but webhooks keep reservations in sync with events we
don't initiate (pending captures resolving, disputes/chargebacks, PayPal auto-voiding an
expired hold, or a dropped approval response). The receiver is `POST /paypal/webhook`.

To enable:
1. PayPal dashboard → your app → Webhooks → add
   `https://carfinder1-….herokuapp.com/paypal/webhook`.
2. Subscribe to: PAYMENT.AUTHORIZATION.CREATED, PAYMENT.AUTHORIZATION.VOIDED,
   PAYMENT.CAPTURE.COMPLETED, PAYMENT.CAPTURE.PENDING, PAYMENT.CAPTURE.DENIED,
   PAYMENT.CAPTURE.REFUNDED, CUSTOMER.DISPUTE.CREATED.
3. Copy the generated **Webhook ID** into the Heroku config var `PAYPAL_WEBHOOK_ID`.

With `PAYPAL_WEBHOOK_ID` set, every event's signature is verified before it's applied
and each change is written to the reservation's audit trail (as `paypal-webhook`).
Without it, every webhook is **rejected** (400), because an unverified event could
fake a capture or refund. For local testing only you can set
`PAYPAL_WEBHOOK_ALLOW_UNVERIFIED=true`.

## Notes

- Payment buttons show a banner "PayPal not configured — records intent only" until
  step 2's PayPal keys are set; after that they charge for real.
- The online app places real auth holds with PayPal card fields: `GET /online-config`
  (client id + env) inits the JS SDK, `POST /quote` shows the price breakdown,
  `POST /paypal/create-order` (`job_id`, `vehicle_id`, `insurance`) creates an order for
  the server-computed amount and pre-assigns the reservation id (PayPal `custom_id`), the
  buyer approves, and the app sends `paypal_order_id` to `POST /requests` — the backend
  checks the order matches the quote, authorizes the hold, and verifies the held amount.
  Retrying `/requests` with the same order returns the same reservation (no duplicates).
- **Cancel** in the portal voids an active card hold automatically; the daily
  `capture-due` job also skips cancelled reservations.
- On a full cancellation, the portal reminds the agent to also clawback the rentalcover
  net + cancel the policy (rentalcover API integration is the remaining Phase 2 piece).
