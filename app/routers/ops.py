"""Operational endpoints: metrics snapshot and the drift canary."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..config import Settings, get_settings
from ..guard import is_verified
from ..metrics import get_metrics
from ..services.canary import run_canary
from ..services.stats_service import run_stats

router = APIRouter(tags=["ops"])


@router.get("/stats")
async def stats(
    request: Request,
    lat: float,
    lon: float,
    city: str = "",
    force: bool = False,
    cache_only: bool = False,
    settings: Settings = Depends(get_settings),
) -> dict:
    """City aggregates for the SEO generator. Cache-first (Redis) — only hits the
    upstream when cold, so generating pages doesn't burn booking credits.

    Credit guard: unverified callers (not one of our pages/apps, no API key) are
    served from cache only — they can never trigger a fresh upstream pull, and
    `force` is ignored for them. Any caller may pass cache_only=true (the online app
    does, for its "from $X/day" tiles) to read without ever spending a credit."""
    verified = is_verified(request, settings)
    return await run_stats(
        lat, lon, city or None,
        force=(force and verified),
        cache_only=(cache_only or not verified),
    )


@router.get("/metrics")
async def metrics(token: str = Query(default="")) -> dict:
    """In-process metrics: per-endpoint latency, cache hit rate, upstream error
    rate, and last-seen upstream quota. Per-dyno; resets on restart.

    Requires ?token=<METRICS_TOKEN> (or CANARY_TOKEN if no metrics token is set).
    With neither configured the endpoint is hidden (404)."""
    settings: Settings = get_settings()
    expected = settings.metrics_token or settings.canary_token
    if not expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token.")
    return get_metrics().snapshot()


@router.get("/canary")
async def canary(token: str = Query(default="")) -> dict:
    """Run one live search and assert the response still parses. Schedule this
    (e.g. hourly) so schema drift pages you before users hit empty results.

    Consumes one upstream call per invocation. If CANARY_TOKEN is set, it must be
    supplied as ?token=... so the endpoint can't be spammed to drain quota."""
    settings: Settings = get_settings()
    if settings.canary_token and not secrets.compare_digest(token, settings.canary_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid canary token.")

    result = await run_canary()
    # 200 when healthy, 503 when the canary is red — so uptime checks can alert.
    if not result.get("ok"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result)
    return result
