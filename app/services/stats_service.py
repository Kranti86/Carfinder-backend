"""City stats for the SEO page generator.

Aggregates a city's live inventory into a few facts (cheapest/day, car count,
suppliers, average price by type, pickup areas) and caches them in Redis. Two
things keep this from burning booking-API credits:

  1. Every REAL customer search writes its city's stats to this cache for free
     (`store_from_cards`, called from search_service). Popular cities stay fresh
     at zero extra cost.
  2. `run_stats` only calls the upstream when the cache is COLD — so the nightly
     page generator mostly reads cache, and cold cities cost one call each,
     throttled by how many pages you regenerate.

SEO pages don't need minute-accurate prices, so the 24h TTL is plenty.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..cache import get_cache
from ..config import get_settings
from ..utils import utcnow_iso

logger = logging.getLogger("carfinder.stats")

_TTL = 60 * 60 * 24          # 24h
STATS_DAYS = 3               # standard window for the "from $/day" figure


def _key(lat: float, lon: float) -> str:
    return f"citystats:v1:{lat:.3f}:{lon:.3f}"


def compute(cards, total_count, currency: str = "USD", days: int = STATS_DAYS) -> dict:
    priced = [c for c in cards if c.price is not None]
    cheapest = min((c.price for c in priced), default=None)

    by: dict[str, list[float]] = {}
    for c in priced:
        by.setdefault(c.vehicle_type or "Other", []).append(c.price)
    avg_by_type = {t: round(sum(v) / len(v) / days) for t, v in sorted(by.items())}

    suppliers = sorted({c.supplier for c in cards if c.supplier})
    pickups: list[str] = []
    for c in cards:
        if c.pickup_location and c.pickup_location not in pickups:
            pickups.append(c.pickup_location)

    return {
        "currency": currency,
        "car_count": total_count or len(cards),
        "cheapest_total": round(cheapest, 2) if cheapest is not None else None,
        "cheapest_per_day": round(cheapest / days) if cheapest is not None else None,
        "avg_by_type": avg_by_type,
        "suppliers": suppliers,
        "supplier_count": len(suppliers),
        "top_pickups": pickups[:6],
        "days": days,
        "updated_at": utcnow_iso(),
    }


async def store_from_cards(lat, lon, cards, total_count, currency="USD") -> None:
    """Piggyback writer — called after every real search. Never raises."""
    try:
        await get_cache().set_json(_key(lat, lon), compute(cards, total_count, currency), _TTL)
    except Exception as exc:  # noqa: BLE001
        logger.debug("stats piggyback store failed: %s", exc)


async def run_stats(lat: float, lon: float, city: str | None = None,
                    force: bool = False, cache_only: bool = False) -> dict:
    """Return a city's stats — cache-first; only calls the upstream when cold.

    cache_only=True (unverified callers): serve whatever is cached and NEVER call the
    upstream, so a scraper can't make us spend a booking credit on a cold city.
    """
    cache = get_cache()
    if cache_only or not force:
        cached = await cache.get_json(_key(lat, lon))
        if cached:
            return {**cached, "city": city} if city else cached
    if cache_only:
        # cold cache + unverified → return empty rather than spending a credit
        return {"city": city} if city else {}

    # Cold cache → one standard search, then cache it.
    from ..models import SearchQuery
    from ..providers.rapidapi_booking import BookingRapidAPIProvider

    now = datetime.now(timezone.utc)
    pu = (now + timedelta(days=30)).strftime("%Y-%m-%dT10:00")
    do = (now + timedelta(days=30 + STATS_DAYS)).strftime("%Y-%m-%dT10:00")
    q = SearchQuery(
        lat=lat, lon=lon, pickup_datetime=pu, dropoff_datetime=do,
        country=get_settings().default_country_of_residence,
    )
    from ..budget import BudgetExhausted, spend_credit
    try:
        await spend_credit()
    except BudgetExhausted:
        return {"city": city} if city else {}
    _key_, cards, count = await BookingRapidAPIProvider().search_full(q)
    stats = compute(cards, count)
    await cache.set_json(_key(lat, lon), stats, _TTL)
    return {**stats, "city": city} if city else stats
