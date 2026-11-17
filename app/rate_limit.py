"""Server-side rate limiting (review §5).

Client-side debounce is bypassable, so /suggest and /search are limited here too.
Counters live in Redis (shared across dynos) via a fixed-window counter. We key on
IP *and* an optional session id so a single shared NAT doesn't starve everyone and
a single session can't rotate its way around the limit.

Fail-open: if the counter backend hiccups we allow the request rather than block
real users — quota protection is a separate, dedicated concern (the breaker +
upstream quota headers), not this limiter.
"""

from __future__ import annotations

from fastapi import Request

from .cache import get_cache


def client_ip(request: Request) -> str:
    """The caller's IP. Railway's edge rewrites X-Forwarded-For with the real client
    address (verified live: a forged value is replaced), so its first entry is trusted."""
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def _client_id(request: Request, session_id: str | None) -> str:
    # Limits are keyed on the IP ONLY. An earlier version mixed in a client-supplied
    # session id, which let anyone get a fresh limit by sending a new X-Session-Id on
    # every request (found in the Nov 2026 security review). session_id is ignored.
    return client_ip(request)


async def check_rate_limit(
    request: Request,
    *,
    bucket: str,
    limit: int,
    window: int,
    session_id: str | None = None,
) -> bool:
    """Return True if the request is allowed, False if it should be 429'd."""
    if limit <= 0:
        return True
    client = _client_id(request, session_id)
    key = f"ratelimit:{bucket}:{client}"
    count = await get_cache().incr_with_ttl(key, window)
    if count == 0:  # backend failure → fail open
        return True
    return count <= limit
