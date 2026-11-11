"""/health — liveness + dependency visibility."""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from ..cache import get_cache
from ..config import get_settings
from ..providers import airports

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    cache = get_cache()
    return {
        "status": "ok",
        "version": __version__,
        "cache": {
            "backend": "redis" if cache.using_redis else "in-memory",
            "healthy": await cache.healthy(),
        },
        "providers": {
            "mapbox_configured": settings.mapbox_enabled,
            "rapidapi_configured": settings.rapidapi_enabled,
        },
        "airports_loaded": airports.count(),
    }
