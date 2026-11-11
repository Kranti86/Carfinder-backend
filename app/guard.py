"""Credit guard — decide who is allowed to spend a booking-API credit.

Where credits are spent: POST /search (a real upstream search) and a COLD /stats.
CORS keeps *browsers on other sites* out, but a scraper ignores CORS, so this adds a
server-side check that CORS can't give us:

  * Verified caller = request carries an Origin/Referer on one of our own domains,
    OR a valid X-API-Key (issued to an approved AI-assistant / partner).
  * /search  : verified only, else 403.  (Still rate-limited by the existing limiter.)
  * /stats   : anyone may READ, but unverified callers are forced to cache_only —
               they can never trigger a fresh upstream pull, and `force` is ignored.

This means a scraper can look at cached public numbers (harmless, even good for SEO),
but can never make you pay for a live search.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request

from .config import Settings


def _host(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "//" + url
    return (urlparse(url).netloc or "").split(":")[0].lower()


def _api_keys(settings: Settings) -> set[str]:
    raw = getattr(settings, "guard_api_keys", "") or ""
    return {k.strip() for k in raw.split(",") if k.strip()}


def _allow_domains(settings: Settings) -> list[str]:
    raw = getattr(settings, "guard_allow_domains", "") or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def is_verified(request: Request, settings: Settings) -> bool:
    """True if the caller is one of our own pages/apps or a keyed partner."""
    if not getattr(settings, "guard_enabled", True):
        return True
    key = request.headers.get("x-api-key", "")
    if key and key in _api_keys(settings):
        return True
    host = _host(request.headers.get("origin") or request.headers.get("referer") or "")
    if not host:
        return False
    for d in _allow_domains(settings):
        if host == d or host.endswith("." + d):
            return True
    return False
