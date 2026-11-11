"""Daily ceiling on paid booking-API calls (UPSTREAM_DAILY_CAP).

The credit guard trusts the Origin/Referer header, which a script can forge, and
per-IP rate limits can be spread across many IPs. This is the backstop: once the
day's budget is spent, no caller — verified or not — can trigger another paid call
until the next UTC day. Cached results keep being served.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .cache import get_cache
from .config import get_settings
from .metrics import get_metrics

logger = logging.getLogger("carfinder.budget")


class BudgetExhausted(RuntimeError):
    pass


async def spend_credit() -> None:
    """Count one upstream call against today's cap; raise BudgetExhausted if over."""
    cap = get_settings().upstream_daily_cap
    if cap <= 0:
        return
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    count = await get_cache().incr_with_ttl(f"upstream_budget:{day}", 60 * 60 * 26)
    if count == 0:  # counter backend hiccup → fail open, like the rate limiter
        return
    if count > cap:
        get_metrics().incr("upstream_budget_exhausted")
        logger.error("upstream daily cap reached (%s) — refusing paid call", cap)
        raise BudgetExhausted("daily upstream budget exhausted")
