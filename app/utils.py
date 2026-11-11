"""Small shared helpers."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string, seconds precision.

    Used to stamp when prices were fetched, so the UI can honestly surface
    'prices as of X minutes ago' (architecture doc, correctness note §4).
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two lat/lon points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
