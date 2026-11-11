"""/suggest — location autocomplete.

Cheap and fast by design: cached for weeks (coords don't move), rate-limited
server-side (client debounce is bypassable — review §5), and it never touches the
inventory quota.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from ..cache import get_cache, suggest_key
from ..config import Settings, get_settings
from ..models import Suggestion
from ..providers import geocoding
from ..rate_limit import check_rate_limit
from .errors import rate_limited, upstream_unavailable

logger = logging.getLogger("carfinder.suggest")
router = APIRouter(tags=["suggest"])


@router.get("/suggest", response_model=list[Suggestion])
async def suggest(
    request: Request,
    q: str = Query(..., min_length=3, description="Partial location text (min 3 chars)"),
    settings: Settings = Depends(get_settings),
) -> list[Suggestion]:
    allowed = await check_rate_limit(
        request,
        bucket="suggest",
        limit=settings.suggest_rate_limit,
        window=settings.suggest_rate_window,
    )
    if not allowed:
        raise rate_limited()

    cache = get_cache()
    key = suggest_key(q)
    cached = await cache.get_json(key)
    if cached is not None:
        return [Suggestion(**s) for s in cached]

    try:
        suggestions = await geocoding.suggest(q)
    except Exception as exc:  # noqa: BLE001
        logger.warning("suggest failed for %r: %s", q, exc)
        raise upstream_unavailable("geocoding") from exc

    await cache.set_json(
        key, [s.model_dump() for s in suggestions], settings.suggest_ttl_seconds
    )
    return suggestions
