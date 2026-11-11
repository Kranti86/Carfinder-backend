"""Redis-backed circuit breaker.

State lives in Redis so a breaker tripped by one dyno protects the shared
upstream quota across ALL dynos (review §3). If Redis is unavailable the breaker
fails OPEN-permissive (never blocks real traffic on a cache hiccup) but logs it.

States:
  closed     — normal; failures accumulate toward the threshold
  open       — upstream is considered down; calls are rejected fast
  half-open  — after the reset window, ONE probe is allowed through
"""

from __future__ import annotations

import logging

from .cache import get_cache
from .config import get_settings

logger = logging.getLogger("carfinder.breaker")


class CircuitOpenError(RuntimeError):
    """Raised when the breaker is open and a call is rejected without dialling."""


class CircuitBreaker:
    def __init__(self, name: str) -> None:
        self.name = name
        self._settings = get_settings()
        self._cache = get_cache()

    @property
    def _fail_key(self) -> str:
        return f"breaker:{self.name}:failures"

    @property
    def _open_key(self) -> str:
        return f"breaker:{self.name}:open"

    async def allow(self) -> bool:
        """Return True if a call may proceed. Consumes the half-open probe slot."""
        open_flag = await self._cache.get_raw(self._open_key)
        if open_flag is None:
            return True  # closed (or open-key expired → half-open probe allowed)
        # Breaker is open. The open key has a TTL == reset window; once it expires
        # the next caller gets through as the half-open probe.
        return False

    async def record_success(self) -> None:
        await self._cache.set_raw(self._fail_key, "0")
        await self._cache.set_raw(self._open_key, "", ttl=1)  # effectively clear

    async def record_failure(self) -> None:
        count = await self._cache.incr_with_ttl(
            self._fail_key, self._settings.breaker_reset_seconds * 4
        )
        if count >= self._settings.breaker_fail_threshold:
            await self._cache.set_raw(
                self._open_key, "1", ttl=self._settings.breaker_reset_seconds
            )
            logger.warning(
                "circuit '%s' OPEN after %d consecutive failures", self.name, count
            )

    async def guard(self) -> None:
        """Raise CircuitOpenError if the breaker is currently open."""
        if not await self.allow():
            raise CircuitOpenError(f"circuit '{self.name}' is open")
