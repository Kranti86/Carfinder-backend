# CarFinder — Backend API

Car-rental **discovery** API: location autocomplete (`/suggest`) + job-based car
search (`/search`), sourced live from Booking.com car rentals via RapidAPI, with
Mapbox for geocoding.

This backend is built to the improved architecture from the review — job-based
search, a versioned all-dimensions cache key, Redis-shared state, and an
anti-corruption normalizer that isolates upstream schema drift.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # optional
pip install -r requirements.txt

cp .env.example .env                                    # then fill in real keys
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for interactive API docs.

You need two keys in `.env` for live data:

- `MAPBOX_TOKEN` — https://account.mapbox.com/access-tokens
- `RAPIDAPI_KEY` — from your RapidAPI Booking.com listing (also set `RAPIDAPI_HOST`
  / `RAPIDAPI_BASE_URL` to match that listing)

Without keys the app still boots: `/health` works, `/suggest` returns 503, and a
`/search` job resolves to a clean `error` state. Redis is optional locally — it
falls back to an in-memory cache (dev only; not shared across dynos).

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + which providers/cache are configured |
| GET | `/suggest?q=` | Location autocomplete (ZIP/city/airport), cached for weeks |
| POST | `/search` | Start a search → returns `{job_id, status:"pending"}` (202) |
| GET | `/search/{job_id}` | Poll a search job → `{status, cards, priced_at, ...}` |
| GET | `/vehicle/{search_key}/{vehicle_id}` | Vehicle detail lookup |
| GET | `/supplier/{search_key}/{vehicle_id}` | Supplier detail (`?tab=details`) |
| GET | `/booking-summary/{search_key}/{vehicle_id}` | Booking summary / price breakdown |
| GET | `/metrics` | Per-endpoint latency, cache hit rate, upstream error rate, quota |
| GET | `/canary` | Runs one live search, asserts it still parses (200 ok / 503 red) |

Upstream is the RapidAPI **Booking.com "Car Rental - V2"** API
(`/api/v2/cars/...` on `booking-com15.p.rapidapi.com`). `searchCarRentals` uses
snake_case params; `vehicleDetails` / `supplierDetails` / `bookingSummary` use
`searchKey` + `vehicleId`. Currency is derived from `countryOfResidence`, not a
currency param.

### Why job-based search

A full search takes 4–11s (the upstream waits for the slowest supplier). Holding
an HTTP connection that long risks Heroku's 30s router timeout, especially with a
retry. So `POST /search` returns a `job_id` immediately and runs the upstream call
in the background; the client polls `GET /search/{job_id}` until `status` is `done`
or `error`. The upstream returns one aggregated blob, so a job flips
pending → done in a single step (there's no true per-supplier streaming to expose).

### Search request body

```json
{
  "lat": 25.7959,
  "lon": -80.287,
  "pickup_datetime": "2026-08-01T10:00",
  "dropoff_datetime": "2026-08-04T10:00",
  "currency": "USD",
  "country": "us",
  "dropoff_lat": null,
  "dropoff_lon": null,
  "driver_age": null,
  "sort": "cheapest"
}
```

`dropoff_lat`/`dropoff_lon` make it a one-way rental; `currency`, `country`, and
`driver_age` also affect results — all of these are part of the cache key so
different searches never collide. `sort` (see below) is the one field that is NOT
in the cache key. Dates are sent upstream as `YYYY-MM-DD` and times as `HH:MM`.

### Ranking (`sort`)

Results are ranked in-memory after the fetch, so it adds no latency and every sort
mode reuses the same cached upstream call (`sort` is intentionally NOT in the cache
key). Add `"sort": "..."` to the search body:

| mode | ordering |
|---|---|
| `cheapest` (default) | price ascending, ties broken by nearest pickup |
| `closest` | nearest pickup ascending, ties broken by price |
| `best_per_type` | collapse to the single cheapest card per vehicle type |

In every mode each card is annotated with `distance_km` (great-circle distance from
the search pickup coords) and `best_in_type` — `true` on the cheapest card within
its `vehicle_type`, so the UI can surface "best Economy / best SUV / …" without
dropping the other cards. Cards missing a price (or pickup coords) always sort last.
Vehicle classes are normalized into stable buckets (Economy, Compact, SUV, Van, …)
in the provider normalizer.

---

## Project layout

```
carfinder/
├── app/
│   ├── main.py                 # FastAPI app + CORS + lifecycle
│   ├── config.py               # all settings from env (pydantic-settings)
│   ├── models.py               # clean domain types (the anti-corruption boundary)
│   ├── cache.py                # Redis + in-memory fallback; versioned cache keys
│   ├── jobs.py                 # Redis-backed job store for submit-then-poll search
│   ├── circuit_breaker.py      # Redis-backed breaker (shared across dynos)
│   ├── rate_limit.py           # server-side rate limiting
│   ├── utils.py
│   ├── providers/
│   │   ├── http.py             # shared httpx client: timeout + retry + breaker
│   │   ├── geocoding.py        # Mapbox → Suggestion (Mapbox shape lives ONLY here)
│   │   ├── airports.py         # IATA enrichment from data/airports.csv
│   │   ├── base.py             # InventoryProvider interface
│   │   └── rapidapi_booking.py # Booking/RapidAPI + normalizer (upstream shape lives ONLY here)
│   ├── services/
│   │   ├── search_service.py   # cache-first search + request coalescing
│   │   └── ranking.py          # cheapest → closest, best-in-type flagging
│   └── routers/
│       ├── suggest.py  search.py  health.py  errors.py
├── data/
│   ├── airports.csv            # ~58 busiest US airports (regenerate full set: data/README.md)
│   └── README.md
├── tests/                      # cache-key + normalizer tests (the review-critical bits)
├── requirements.txt  Procfile  runtime.txt  .env.example  .gitignore
```

---

## Architecture decisions baked in (from the review)

- **Versioned, all-dimensions cache key** (`app/cache.py`) — coords/time are
  rounded/snapped for shareability; currency, one-way drop-off, and driver age are
  included verbatim so genuinely-different searches never share a cache entry. Bump
  `SEARCH_KEY_VERSION` to invalidate the namespace after a logic change.
- **Job-based search** (`app/jobs.py`, `app/routers/search.py`) — decouples the
  slow upstream call from the client's HTTP request.
- **Redis-shared state** — cache, jobs, circuit breaker, and rate-limit counters
  all live in Redis so behaviour is correct across multiple dynos. In-memory
  fallback is dev-only and logs loudly.
- **Anti-corruption normalizer** (`app/providers/rapidapi_booking.py`) — the only
  place that knows the upstream's raw JSON. Pinned to the real V2 response: cards
  are read from `data.content.items` (filtering out non-`CAR_CARD` entries),
  prices parsed from `$`-strings, vehicle class derived from the `subtitle`, and
  pickup coordinates joined from `data.map.supplierLocations` by location name.
  `tests/test_normalizer.py` locks this to the verified shape.
- **Server-side rate limiting + circuit breaker + bounded retries** protect the
  shared upstream quota independently of the client-side debounce.

---

## Reliability & ops

- **Transparent `search_key` expiry recovery.** The `search_key` is a signed,
  short-lived blob. When a user opens a detail/supplier/booking lookup after it
  has expired, the provider decodes the stale key, re-runs the search to mint a
  fresh one, and retries once. If that still fails it raises a clean **410 Gone**
  ("please search again") instead of a confusing 5xx. (`app/providers/rapidapi_booking.py`)
- **Drift canary** (`/canary`, `app/services/canary.py`). Runs one live search and
  asserts cards parse, a price is non-null, and a `search_key` came back — returning
  200 when healthy and **503 with a diagnosis** when red. Point an uptime check or
  a scheduled job at it (hourly) so the next schema drift alerts you before users
  see empty results. Set `CANARY_TOKEN` and call `/canary?token=...` to keep it from
  being spammed. On Heroku: `heroku addons:create scheduler:standard` →
  `curl -fsS https://<app>/canary?token=$CANARY_TOKEN`.
- **Metrics** (`/metrics`). Per-endpoint latency (keyed by route template, not raw
  path), cache hit rate, upstream call/error rate, and last-seen RapidAPI quota from
  response headers. Every response also carries an `x-response-time-ms` header.
  Per-process (per-dyno); wire to a shared backend for fleet aggregates.

## Tests

```bash
pytest -q
```

Covers what's most likely to silently break: the cache key (`test_cache_key.py`),
schema-drift normalization against the real response (`test_normalizer.py`),
result ranking (`test_ranking.py`), `search_key` decoding (`test_search_key.py`),
and the metrics/canary plumbing.

---

## Deploy (Heroku)

```bash
heroku create
heroku addons:create heroku-redis:mini          # sets REDIS_URL
heroku config:set MAPBOX_TOKEN=... RAPIDAPI_KEY=... RAPIDAPI_HOST=... RAPIDAPI_BASE_URL=...
git push heroku main
```

Use Basic dynos, not Eco — Eco sleeps and adds 10–20s cold starts on top of an
already-slow search.

---

## Not yet built (deliberately)

Request-only booking flow (form + Telegram/Slack notification + Postgres, with
persist-first durability and idempotency), the funnel analytics, result
pagination (the upstream returns ~50 of N results per call), and the Next.js
frontend. These are the next build steps — the backend is structured so they slot
in without touching the provider or cache layers.
