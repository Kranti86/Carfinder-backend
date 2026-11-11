"""Search orchestration: cache-first, then upstream, with request coalescing.

Flow (review §2 job model lives in the router; this is the actual work):
  1. Build the versioned cache key from the full query.
  2. Cache hit → return instantly, flagged cached=True with the original priced_at.
  3. Cache miss → try to claim a short-lived coalescing lock so that N identical
     concurrent searches make ONE upstream call, not N (review "request coalescing").
     The losers briefly wait for the winner's result to land in cache.
  4. Winner calls the provider, normalizes, writes to cache with priced_at now.
"""

from __future__ import annotations

import asyncio
import logging

from ..budget import spend_credit
from ..cache import get_cache, search_key
from ..config import get_settings
from ..metrics import get_metrics
from ..models import CarCard, SearchQuery
from ..providers.rapidapi_booking import BookingRapidAPIProvider
from ..utils import utcnow_iso

logger = logging.getLogger("carfinder.search")

_provider = BookingRapidAPIProvider()


class SearchResult:
    def __init__(self, search_key: str | None, cards: list[CarCard], cached: bool, priced_at: str):
        self.search_key = search_key
        self.cards = cards
        self.cached = cached
        self.priced_at = priced_at


async def _read_cache(key: str) -> SearchResult | None:
    data = await get_cache().get_json(key)
    if not data:
        return None
    return SearchResult(
        search_key=data.get("search_key"),
        cards=[CarCard(**c) for c in data.get("cards", [])],
        cached=True,
        priced_at=data.get("priced_at", ""),
    )


async def _write_cache(key: str, result: SearchResult, ttl: int) -> None:
    await get_cache().set_json(
        key,
        {
            "search_key": result.search_key,
            "cards": [c.model_dump() for c in result.cards],
            "priced_at": result.priced_at,
        },
        ttl,
    )


async def run_search(query: SearchQuery) -> SearchResult:
    settings = get_settings()
    cache = get_cache()
    key = search_key(query)

    hit = await _read_cache(key)
    if hit is not None:
        get_metrics().incr("cache_hits")
        logger.info("search cache HIT %s", key)
        return hit
    get_metrics().incr("cache_misses")

    # --- request coalescing -------------------------------------------------
    lock_key = f"{key}:lock"
    got_lock = await _try_lock(lock_key, ttl=settings.upstream_timeout_seconds + 5)
    if not got_lock:
        # Someone else is already fetching this exact search. Briefly poll cache.
        for _ in range(int((settings.upstream_timeout_seconds + 5) * 4)):
            await asyncio.sleep(0.25)
            hit = await _read_cache(key)
            if hit is not None:
                logger.info("search coalesced HIT %s", key)
                return hit
        # Winner never landed a result in time — fall through and fetch ourselves.

    try:
        await spend_credit()  # daily ceiling on paid calls (raises when exhausted)
        skey, cards, count = await _provider.search_full(query)
        result = SearchResult(
            search_key=skey, cards=cards, cached=False, priced_at=utcnow_iso()
        )
        await _write_cache(key, result, settings.search_ttl_seconds)
        # Piggyback: refresh this city's SEO stats for free off a real search.
        try:
            from .stats_service import store_from_cards
            await store_from_cards(query.lat, query.lon, cards, count)
        except Exception:  # noqa: BLE001
            pass
        return result
    finally:
        await cache.set_raw(lock_key, "", ttl=1)  # release lock promptly


async def _try_lock(lock_key: str, ttl: float) -> bool:
    """Best-effort SET-if-absent. On the in-memory fallback this is not atomic
    across processes, which is fine — coalescing is an optimization, not a
    correctness requirement."""
    cache = get_cache()
    existing = await cache.get_raw(lock_key)
    if existing:
        return False
    await cache.set_raw(lock_key, "1", ttl=int(ttl))
    return True
