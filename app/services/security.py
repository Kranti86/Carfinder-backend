"""Security helpers: two-factor codes for admin logins, bot checks, and Slack alerts
for suspicious activity (sign-ins, lockouts, search budget, card-decline bursts)."""

from __future__ import annotations

import logging
import os

import httpx
import pyotp

from ..cache import get_cache
from . import notify

logger = logging.getLogger("carfinder.security")

ISSUER = "Therentalcars Ops"


# ------------------------------------------------------------------ two-factor (TOTP)

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def totp_ok(secret: str, code: str) -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)   # ±30s clock drift


async def totp_fresh(agent_id: int, code: str) -> bool:
    """Refuse a code that was already used (replay of a code seen over someone's shoulder)."""
    key = f"totp_used:{agent_id}:{code}"
    cache = get_cache()
    if await cache.get_raw(key):
        return False
    await cache.set_raw(key, "1", ttl=120)
    return True


def qr_svg(data: str) -> str:
    import segno
    return segno.make(data, error="m").svg_inline(scale=5, dark="#0b1d3a", light="#ffffff")


# ------------------------------------------------------------------ alerts

async def alert_once(key: str, text: str, ttl: int = 900) -> None:
    """Post a Slack alert at most once per `ttl` seconds for the same key."""
    try:
        cache = get_cache()
        if await cache.get_raw(f"alert:{key}"):
            return
        await cache.set_raw(f"alert:{key}", "1", ttl=ttl)
        await notify.slack(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("security alert failed: %s", exc)


async def count_and_alert(counter: str, window: int, threshold: int, text: str) -> int:
    """Count an event in a rolling window; alert once when it reaches `threshold`."""
    try:
        n = await get_cache().incr_with_ttl(f"secctr:{counter}", window)
        if n >= threshold:
            await alert_once(f"ctr:{counter}", text.format(n=n), ttl=window)
        return n
    except Exception:  # noqa: BLE001
        return 0


def ua_short(ua: str) -> str:
    ua = ua or ""
    for name in ("Edg", "Chrome", "Firefox", "Safari"):
        if name in ua:
            os_ = next((o for o in ("Windows", "Mac OS", "Android", "iPhone", "Linux") if o in ua), "")
            return f"{'Edge' if name == 'Edg' else name}{' on ' + os_ if os_ else ''}"
    return ua[:40] or "unknown browser"


# ------------------------------------------------------------------ bot check (Cloudflare Turnstile)

def turnstile_enabled() -> bool:
    return bool(os.environ.get("TURNSTILE_SECRET_KEY"))


async def turnstile_ok(token: str | None, ip: str = "") -> bool:
    """True when the bot check passes, or when Turnstile isn't configured."""
    secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
    if not secret:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                             data={"secret": secret, "response": token, "remoteip": ip})
        return bool(r.json().get("success"))
    except Exception as exc:  # noqa: BLE001
        # Cloudflare unreachable: let real customers through rather than block sales.
        logger.warning("turnstile verify failed open: %s", exc)
        return True


HUMAN_PASS_TTL = 1800      # one passed check covers 30 minutes of searching


async def human_check(request, response) -> bool:
    """Bot check for search/checkout. Off (always True) until TURNSTILE_SECRET_KEY is set.

    A visitor sends either a fresh Turnstile token (X-Turnstile-Token) or the pass we
    gave them after their last check (X-Human-Pass, bound to their IP, 30 minutes), so
    real customers solve at most one invisible check per half hour."""
    if not turnstile_enabled():
        return True
    import secrets as _secrets
    from ..rate_limit import client_ip
    ip = client_ip(request)
    cache = get_cache()
    p = request.headers.get("x-human-pass", "")
    if p and await cache.get_raw(f"humanpass:{p}") == ip:
        return True
    if await turnstile_ok(request.headers.get("x-turnstile-token", ""), ip):
        new = _secrets.token_urlsafe(24)
        await cache.set_raw(f"humanpass:{new}", ip, ttl=HUMAN_PASS_TTL)
        response.headers["X-Human-Pass"] = new
        return True
    return False
