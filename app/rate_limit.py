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


def _client_id(request: Request, session_id: str | None) -> str:
    # X-Forwarded-For first hop is the real client behind Heroku's router.
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    sid = session_id or request.headers.get("x-session-id", "")
    return f"{ip}|{sid}" if sid else ip


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
