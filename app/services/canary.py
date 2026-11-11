"""Schema-drift canary (architecture doc §9).

Runs one real search against a known location and asserts the response still
parses: cards present, at least one price non-null, a search_key returned. A
scheduled task hits /canary periodically; when the upstream shape drifts, the
canary goes red before real users see empty results.

It bypasses the cache on purpose (calls the provider directly) so it exercises
the live upstream + normalizer every time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..metrics import get_metrics
from ..models import SearchQuery
from ..providers.rapidapi_booking import BookingRapidAPIProvider

logger = logging.getLogger("carfinder.canary")

_provider = BookingRapidAPIProvider()


async def run_canary() -> dict:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    pickup = (now + timedelta(days=30)).strftime("%Y-%m-%dT10:00")
    dropoff = (now + timedelta(days=33)).strftime("%Y-%m-%dT10:00")

    query = SearchQuery(
        lat=settings.canary_lat,
        lon=settings.canary_lon,
        pickup_datetime=pickup,
        dropoff_datetime=dropoff,
        country=settings.default_country_of_residence,
    )

    result: dict = {"ok": False, "checked_at": now.replace(microsecond=0).isoformat()}
    try:
        search_key, cards = await _provider.search(query)
        priced = [c for c in cards if c.price is not None]
        typed = [c for c in cards if c.vehicle_type != "Other"]
        result.update(
            {
                "ok": bool(cards) and bool(priced) and bool(search_key),
                "num_cards": len(cards),
                "num_priced": len(priced),
                "num_typed": len(typed),
                "has_search_key": bool(search_key),
                "sample": cards[0].model_dump() if cards else None,
            }
        )
        if not result["ok"]:
            result["reason"] = _diagnose(cards, priced, search_key)
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
        logger.warning("canary error: %s", exc)

    get_metrics().set_gauge("canary_ok", 1.0 if result["ok"] else 0.0)
    return result


def _diagnose(cards, priced, search_key) -> str:
    if not cards:
        return "no cards parsed — likely a response-shape drift in content.items"
    if not priced:
        return "cards parsed but all prices are null — pricing field path may have moved"
    if not search_key:
        return "no search_key extracted — key path may have moved"
    return "unknown"
