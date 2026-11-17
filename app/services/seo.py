"""SEO location pages: collect price facts per place and decide what gets published.

Data comes from two places, both cheap:
  1. Every real customer search (free — we already paid for it). `record_search`
     turns the results into price facts for that place and keeps a daily history.
  2. A small daily budget of searches for top US airports (`seed_airports`), so
     those pages exist before customers search them. Each refresh spends one
     upstream credit through the same daily cap as everything else.

A place is published (indexed, listed, in the sitemap) only once it has enough
real data, so Google never sees thin pages.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import session_factory
from ..models_db import CityStat, CityStatDay

logger = logging.getLogger("carfinder.seo")

SEED_TRIP_DAYS = 3            # standard window for seeded airports
SEED_LEAD_DAYS = 30           # seeded searches look 30 days ahead
STALE_AFTER = timedelta(days=3)
MERGE_RADIUS_KM = 4.0         # searches this close count as the same place
MIN_SEARCHES = 3              # real searches before a city page is published
MIN_CARS = 10
MAX_AGE = timedelta(days=45)  # hide pages whose prices are older than this

# Top US rental airports: (IATA, short name, city, region, lat, lon)
AIRPORTS: list[tuple[str, str, str, str, float, float]] = [
    ("MCO", "Orlando International Airport", "Orlando", "Florida", 28.4312, -81.3081),
    ("MIA", "Miami International Airport", "Miami", "Florida", 25.7959, -80.2870),
    ("LAX", "Los Angeles International Airport", "Los Angeles", "California", 33.9416, -118.4085),
    ("LAS", "Harry Reid International Airport", "Las Vegas", "Nevada", 36.0840, -115.1537),
    ("FLL", "Fort Lauderdale-Hollywood International Airport", "Fort Lauderdale", "Florida", 26.0742, -80.1506),
    ("ATL", "Hartsfield-Jackson Atlanta International Airport", "Atlanta", "Georgia", 33.6407, -84.4277),
    ("DEN", "Denver International Airport", "Denver", "Colorado", 39.8561, -104.6737),
    ("DFW", "Dallas Fort Worth International Airport", "Dallas", "Texas", 32.8998, -97.0403),
    ("PHX", "Phoenix Sky Harbor International Airport", "Phoenix", "Arizona", 33.4352, -112.0101),
    ("SFO", "San Francisco International Airport", "San Francisco", "California", 37.6213, -122.3790),
    ("SEA", "Seattle-Tacoma International Airport", "Seattle", "Washington", 47.4502, -122.3088),
    ("ORD", "O'Hare International Airport", "Chicago", "Illinois", 41.9742, -87.9073),
    ("JFK", "John F. Kennedy International Airport", "New York", "New York", 40.6413, -73.7781),
    ("EWR", "Newark Liberty International Airport", "Newark", "New Jersey", 40.6895, -74.1745),
    ("LGA", "LaGuardia Airport", "New York", "New York", 40.7769, -73.8740),
    ("BOS", "Boston Logan International Airport", "Boston", "Massachusetts", 42.3656, -71.0096),
    ("TPA", "Tampa International Airport", "Tampa", "Florida", 27.9755, -82.5332),
    ("SAN", "San Diego International Airport", "San Diego", "California", 32.7338, -117.1933),
    ("IAH", "George Bush Intercontinental Airport", "Houston", "Texas", 29.9902, -95.3368),
    ("CLT", "Charlotte Douglas International Airport", "Charlotte", "North Carolina", 35.2144, -80.9473),
    ("MSP", "Minneapolis-Saint Paul International Airport", "Minneapolis", "Minnesota", 44.8848, -93.2223),
    ("DTW", "Detroit Metropolitan Airport", "Detroit", "Michigan", 42.2162, -83.3554),
    ("PHL", "Philadelphia International Airport", "Philadelphia", "Pennsylvania", 39.8744, -75.2424),
    ("BWI", "Baltimore/Washington International Airport", "Baltimore", "Maryland", 39.1774, -76.6684),
    ("IAD", "Washington Dulles International Airport", "Washington", "Virginia", 38.9531, -77.4565),
    ("SLC", "Salt Lake City International Airport", "Salt Lake City", "Utah", 40.7899, -111.9791),
    ("AUS", "Austin-Bergstrom International Airport", "Austin", "Texas", 30.1975, -97.6664),
    ("BNA", "Nashville International Airport", "Nashville", "Tennessee", 36.1263, -86.6774),
    ("HNL", "Daniel K. Inouye International Airport", "Honolulu", "Hawaii", 21.3245, -157.9251),
    ("OGG", "Kahului Airport", "Maui", "Hawaii", 20.8986, -156.4305),
]


def airport_slug(iata: str, city: str) -> str:
    return f"{slugify(city)}-airport-{iata.lower()}"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:100] or "place"


def km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def trip_days(pickup: str, dropoff: str) -> int:
    """Rental days the way rental desks count them: every started 24h is a day."""
    try:
        a = datetime.fromisoformat(pickup.replace("Z", "+00:00"))
        b = datetime.fromisoformat(dropoff.replace("Z", "+00:00"))
        return max(1, math.ceil((b - a).total_seconds() / 86400))
    except (ValueError, AttributeError):
        return SEED_TRIP_DAYS


def _airport_for(lat, lon):
    best = None
    for a in AIRPORTS:
        d = km(lat, lon, a[4], a[5])
        if d <= MERGE_RADIUS_KM and (best is None or d < best[0]):
            best = (d, a)
    return best[1] if best else None


def _aware(d):
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def _clean_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip())[:200]


async def _find_or_create(s, lat, lon, label) -> CityStat | None:
    ap = _airport_for(lat, lon)
    if ap:
        slug = airport_slug(ap[0], ap[2])
        row = await s.get(CityStat, slug)
        if not row:
            row = CityStat(slug=slug, name=f"{ap[2]} Airport ({ap[0]})", kind="airport",
                           iata=ap[0], region=ap[3], lat=ap[4], lon=ap[5])
            s.add(row)
        return row
    # Non-airport: reuse a nearby place if we already have one.
    rows = (await s.execute(select(CityStat).where(
        CityStat.lat.between(lat - 0.06, lat + 0.06),
        CityStat.lon.between(lon - 0.08, lon + 0.08)))).scalars().all()
    near = [r for r in rows if km(lat, lon, r.lat, r.lon) <= MERGE_RADIUS_KM]
    if near:
        return min(near, key=lambda r: km(lat, lon, r.lat, r.lon))
    name = _clean_label(label)
    if not name or name.lower() in ("your location", "current location"):
        return None
    slug = slugify(name)
    if await s.get(CityStat, slug):
        slug = f"{slug}-{abs(int(lat * 100))}{abs(int(lon * 100))}"[:120]
    row = CityStat(slug=slug, name=name, kind="city", lat=round(lat, 4), lon=round(lon, 4))
    s.add(row)
    return row


async def _save(s, row: CityStat, stats: dict, source: str) -> None:
    now = datetime.now(timezone.utc)
    row.stats = json.dumps(stats)
    row.updated_at = now
    if source == "search":
        row.searches = (row.searches or 0) + 1
    else:
        row.seeded = (row.seeded or 0) + 1
    day = now.strftime("%Y-%m-%d")
    hist = (await s.execute(select(CityStatDay).where(
        CityStatDay.slug == row.slug, CityStatDay.day == day))).scalar_one_or_none()
    cpd = stats.get("cheapest_per_day")
    if hist is None:
        s.add(CityStatDay(slug=row.slug, day=day, cheapest_per_day=cpd,
                          avg_by_type=json.dumps(stats.get("avg_by_type") or {}),
                          car_count=stats.get("car_count") or 0))
    else:
        if cpd is not None and (hist.cheapest_per_day is None or cpd < hist.cheapest_per_day):
            hist.cheapest_per_day = cpd
        hist.avg_by_type = json.dumps(stats.get("avg_by_type") or {})
        hist.car_count = max(hist.car_count or 0, stats.get("car_count") or 0)
    await s.commit()


async def record_search(lat, lon, label, stats: dict, source: str = "search") -> str | None:
    """Store one search's price facts against its place. Never raises."""
    try:
        if not stats or (stats.get("car_count") or 0) < 3:
            return None
        async with session_factory()() as s:
            row = await _find_or_create(s, lat, lon, label)
            if row is None:
                return None
            await _save(s, row, stats, source)
            return row.slug
    except Exception as exc:  # noqa: BLE001
        logger.warning("seo record failed: %s", exc)
        return None


async def seed_airports(limit: int | None = None) -> list[str]:
    """Refresh the stalest top airports, spending at most `limit` upstream credits."""
    from ..budget import BudgetExhausted, spend_credit
    from ..models import SearchQuery
    from ..providers.rapidapi_booking import BookingRapidAPIProvider
    from .stats_service import compute

    limit = int(os.environ.get("SEO_SEED_PER_DAY", "5")) if limit is None else limit
    if limit <= 0:
        return []
    now = datetime.now(timezone.utc)
    async with session_factory()() as s:
        existing = {r.slug: r for r in (await s.execute(select(CityStat))).scalars().all()}

    def age(a):
        r = existing.get(airport_slug(a[0], a[2]))
        return _aware(r.updated_at) if r else datetime(1970, 1, 1, tzinfo=timezone.utc)
    due = [a for a in sorted(AIRPORTS, key=age) if now - age(a) >= STALE_AFTER][:limit]
    done = []
    for iata, _name, city, _region, lat, lon in due:
        try:
            await spend_credit()
        except BudgetExhausted:
            logger.info("seo seed: daily budget reached")
            break
        pu = (now + timedelta(days=SEED_LEAD_DAYS)).strftime("%Y-%m-%dT10:00")
        do = (now + timedelta(days=SEED_LEAD_DAYS + SEED_TRIP_DAYS)).strftime("%Y-%m-%dT10:00")
        try:
            q = SearchQuery(lat=lat, lon=lon, pickup_datetime=pu, dropoff_datetime=do,
                            label=f"{city} Airport ({iata})")
            _k, cards, count = await BookingRapidAPIProvider().search_full(q)
        except Exception as exc:  # noqa: BLE001
            logger.warning("seo seed %s failed: %s", iata, exc)
            continue
        slug = await record_search(lat, lon, q.label, compute(cards, count, days=SEED_TRIP_DAYS),
                                   source="seed")
        if slug:
            done.append(slug)
    return done


# ------------------------------------------------------------------ reading

def stats_of(row: CityStat) -> dict:
    try:
        return json.loads(row.stats or "{}")
    except ValueError:
        return {}


def is_published(row: CityStat, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    st = stats_of(row)
    if (st.get("car_count") or 0) < MIN_CARS or st.get("cheapest_per_day") is None:
        return False
    if now - (_aware(row.updated_at) or now) > MAX_AGE:
        return False
    return (row.searches or 0) >= MIN_SEARCHES or (row.kind == "airport" and (row.seeded or row.searches))


async def published(limit: int | None = None) -> list[CityStat]:
    async with session_factory()() as s:
        rows = (await s.execute(select(CityStat))).scalars().all()
    out = [r for r in rows if is_published(r)]
    out.sort(key=lambda r: (-(r.searches or 0), r.name))
    return out[:limit] if limit else out


async def get_place(slug: str) -> tuple[CityStat | None, list[CityStatDay]]:
    async with session_factory()() as s:
        row = await s.get(CityStat, slug)
        if not row:
            return None, []
        since = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        hist = (await s.execute(select(CityStatDay).where(
            CityStatDay.slug == slug, CityStatDay.day >= since)
            .order_by(CityStatDay.day))).scalars().all()
        return row, list(hist)


async def nearest_stats(lat: float, lon: float, radius_km: float = MERGE_RADIUS_KM) -> dict:
    """Latest saved facts near a point (fallback when the Redis copy has expired)."""
    async with session_factory()() as s:
        rows = (await s.execute(select(CityStat).where(
            CityStat.lat.between(lat - 0.1, lat + 0.1),
            CityStat.lon.between(lon - 0.15, lon + 0.15)))).scalars().all()
    near = [r for r in rows if km(lat, lon, r.lat, r.lon) <= radius_km]
    if not near:
        return {}
    return stats_of(min(near, key=lambda r: km(lat, lon, r.lat, r.lon)))
