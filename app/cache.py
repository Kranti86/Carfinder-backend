"""Shared cache with a versioned key scheme.

Redis is the source of truth so cache, jobs, breaker state, and rate-limit
counters are shared across all dynos. If REDIS_URL is unset or Redis is
unreachable, we fall back to a process-local in-memory store — DEV ONLY, and we
log loudly so a production Redis outage can't hide behind it.

Key-versioning: every namespace carries a version segment (e.g. `search:v1:`).
Bump the version to invalidate an entire namespace atomically after a schema or
key-logic change — no scanning, no manual flush.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from redis.asyncio import Redis

from .config import get_settings

logger = logging.getLogger("carfinder.cache")

# Bump these when the shape of what's stored under a namespace changes.
SUGGEST_KEY_VERSION = "v1"
SEARCH_KEY_VERSION = "v1"


# --------------------------------------------------------------------------- #
# Key builders — the ONLY place cache keys are constructed.
# --------------------------------------------------------------------------- #
def suggest_key(query: str) -> str:
    return f"suggest:{SUGGEST_KEY_VERSION}:{query.strip().lower()}"


def _round_coord(value: float | None) -> str:
    # ~3 decimals ≈ 100m grid, so near-identical searches share a cache entry.
    return "-" if value is None else f"{value:.3f}"


def _snap_time(iso: str) -> str:
    """Snap an ISO datetime down to a 30-minute boundary (string-stable)."""
    # Kept string-based to avoid tz surprises; expects 'YYYY-MM-DDTHH:MM...'.
    try:
        date_part, time_part = iso.split("T", 1)
        hh, mm = time_part[:5].split(":")
        snapped = 0 if int(mm) < 30 else 30
        return f"{date_part}T{hh}:{snapped:02d}"
    except (ValueError, IndexError):
        return iso  # if it doesn't parse, key on it verbatim rather than crash


def search_key(q: "Any") -> str:
    """Build the search cache key from EVERY dimension that affects results.

    Dimensions that are bucketed for shareability (coords, time) are rounded/
    snapped; everything else (currency, one-way drop-off, driver age) is included
    verbatim so two genuinely-different searches never collide. See review §1.
    """
    parts = [
        "search",
        SEARCH_KEY_VERSION,
        _round_coord(q.lat),
        _round_coord(q.lon),
        _round_coord(q.dropoff_lat),
        _round_coord(q.dropoff_lon),
        _snap_time(q.pickup_datetime),
        _snap_time(q.dropoff_datetime),
        (q.currency or "USD").upper(),
        (q.country or "us").lower(),
        str(q.driver_age) if q.driver_age is not None else "-",
    ]
    return ":".join(parts)


# --------------------------------------------------------------------------- #
# Cache backend
# --------------------------------------------------------------------------- #
class _InMemoryStore:
    """Minimal async-compatible fallback. Not shared across processes."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at and expires_at < time.time():
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        expires_at = time.time() + ex if ex else 0.0
        self._data[key] = (expires_at, value)

    async def incr(self, key: str) -> int:
        current = int(await self.get(key) or 0) + 1
        # preserve TTL if present
        item = self._data.get(key)
        ex = None
        if item and item[0]:
            ex = max(1, int(item[0] - time.time()))
        await self.set(key, str(current), ex=ex)
        return current

    async def expire(self, key: str, seconds: int) -> None:
        value = await self.get(key)
        if value is not None:
            await self.set(key, value, ex=seconds)

    async def ping(self) -> bool:
        return True


class Cache:
    """Thin wrapper exposing get/set for JSON values plus raw ops for counters."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._backend: Redis | _InMemoryStore
        self.using_redis = False

        if self._settings.redis_enabled:
            try:
                self._backend = Redis.from_url(
                    self._settings.redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                self.using_redis = True
            except Exception as exc:  # noqa: BLE001
                logger.error("Redis init failed, using in-memory fallback: %s", exc)
                self._backend = _InMemoryStore()
        else:
            logger.warning(
                "REDIS_URL not set — using IN-MEMORY cache. "
                "This is DEV ONLY and is not shared across dynos."
            )
            self._backend = _InMemoryStore()

    # --- JSON helpers -----------------------------------------------------
    async def get_json(self, key: str) -> Any | None:
        try:
            raw = await self._backend.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache get failed (%s): %s", key, exc)
            return None
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self._backend.set(key, json.dumps(value), ex=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache set failed (%s): %s", key, exc)

    # --- raw ops (counters, breaker) --------------------------------------
    async def incr_with_ttl(self, key: str, window: int) -> int:
        """Increment a counter, setting the TTL on first touch. Used by limiter."""
        try:
            count = await self._backend.incr(key)
            if count == 1:
                await self._backend.expire(key, window)
            return count
        except Exception as exc:  # noqa: BLE001
            logger.warning("counter incr failed (%s): %s", key, exc)
            return 0  # fail-open: never block real users on a cache hiccup

    async def get_raw(self, key: str) -> str | None:
        try:
            return await self._backend.get(key)
        except Exception:  # noqa: BLE001
            return None

    async def set_raw(self, key: str, value: str, ttl: int | None = None) -> None:
        try:
            await self._backend.set(key, value, ex=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache set_raw failed (%s): %s", key, exc)

    async def healthy(self) -> bool:
        try:
            return bool(await self._backend.ping())
        except Exception:  # noqa: BLE001
            return False


# Module-level singleton, created lazily.
_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache
