"""App settings (admin-controlled toggles) with a tiny in-process cache.

Currently: `service_fee_enabled` — when False the online funnel runs in
reservation-only mode (no platform markup charged, checkout not gated behind payment).

Reads are cached briefly so the online funnel can call /online-config freely without
hammering the DB. Fail-safe default is True (fees ON) so a DB hiccup never silently
gives away the markup.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from .. import models_db
from ..db import session_factory

_cache: dict[str, tuple[float, str]] = {}
_TTL = 30.0


async def get(key: str, default: str = "") -> str:
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        async with session_factory()() as s:
            row = await s.get(models_db.Setting, key)
            val = row.value if row else default
    except Exception:
        return default
    _cache[key] = (time.time() + _TTL, val)
    return val


async def set(key: str, value: str) -> None:
    async with session_factory()() as s:
        row = await s.get(models_db.Setting, key)
        if row:
            row.value = value
        else:
            s.add(models_db.Setting(key=key, value=value))
        await s.commit()
    _cache[key] = (time.time() + _TTL, value)


async def service_fee_enabled() -> bool:
    return (await get("service_fee_enabled", "true")).lower() != "false"
