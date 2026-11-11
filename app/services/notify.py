"""Slack notifications: instant booking/payment alerts + the daily digest.

Set SLACK_WEBHOOK_URL (a Slack "Incoming Webhook") on every service that should post
(the web app and the daily cron). Without it every call is a silent no-op, and a
Slack failure never breaks a booking or a payment action.

The webhook URL is a secret (anyone holding it can post to the channel), so this
module also scrubs it from httpx's request logs.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("carfinder.notify")

_HOOK_RE = re.compile(r"https://hooks\.slack\.com/[^\s\"']+")
_TZ_NAME = "IST"
_TZ = timezone(timedelta(hours=5, minutes=30))     # owner's timezone for the digest
SYSTEM_ACTORS = {"system", "scheduler", "paypal-webhook"}


class _RedactSlack(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if "hooks.slack.com" in msg:
            record.msg = _HOOK_RE.sub("https://hooks.slack.com/[redacted]", msg)
            record.args = None
        return True


def install_log_redaction() -> None:
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _RedactSlack) for f in lg.filters):
            lg.addFilter(_RedactSlack())


install_log_redaction()


def enabled() -> bool:
    return bool(os.environ.get("SLACK_WEBHOOK_URL"))


async def slack(text: str) -> bool:
    """Post one message. Returns True when Slack accepted it."""
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(url, json={"text": text, "unfurl_links": False})
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001 — never let a notification break anything
        logger.warning("slack notify failed: %s", type(exc).__name__)
        return False


# ---------------------------------------------------------------- formatting

def admin_base() -> str:
    for key in ("ADMIN_BASE_URL", "RAILWAY_SERVICE_CARFINDER_BACKEND_URL", "RAILWAY_PUBLIC_DOMAIN"):
        v = (os.environ.get(key) or "").strip().rstrip("/")
        if v:
            return v if v.startswith("http") else f"https://{v}"
    return ""


def ref_link(r) -> str:
    ref = r.id[:8].upper()
    base = admin_base()
    return f"<{base}/admin/reservations/{r.id}|{ref}>" if base else ref


def _esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pickup(r) -> str:
    return (r.pickup_datetime or "").replace("T", " ")[:16] or "?"


def line(r) -> str:
    """One-line summary of a reservation."""
    return (f"{ref_link(r)} — {_esc(r.customer_name) or 'guest'} · "
            f"{_esc(r.vehicle_name) or '?'} · {_esc(r.city) or '?'} · pickup {_pickup(r)}")


def money(v: float) -> str:
    return f"${(v or 0):,.2f}"


def _aware(d: datetime | None) -> datetime | None:
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _age(d: datetime | None, now: datetime) -> str:
    d = _aware(d)
    if not d:
        return "?"
    mins = int((now - d).total_seconds() // 60)
    if mins < 60:
        return f"{mins}m"
    if mins < 48 * 60:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


def _pickup_dt(r) -> datetime | None:
    raw = (r.pickup_datetime or "").strip()
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- instant alerts

async def new_reservation(r, amount: float) -> None:
    ps = r.payment_status
    if ps == "authorized":
        head = f":white_check_mark: *New booking — card hold approved* ({money(amount)})"
    elif ps == "pending":
        head = f":hourglass_flowing_sand: *New booking — hold under PayPal review* ({money(amount)})"
    elif ps == "failed":
        head = (f":x: *New booking — card DECLINED* ({money(amount)}). "
                f"Call the customer: {_esc(r.customer_phone) or 'no phone'}")
    else:
        head = ":new: *New reservation — no payment taken*"
    await slack(f"{head}\n{line(r)}")


async def payment_event(r, kind: str, detail: str = "") -> None:
    icons = {
        "approved": ":white_check_mark: PayPal approved the card hold",
        "voided": ":warning: Card hold voided / expired",
        "captured": ":moneybag: Payment captured",
        "capture_failed": ":x: Capture FAILED",
        "capture_pending": ":hourglass_flowing_sand: Capture under PayPal review",
        "capture_denied": ":x: Capture DENIED by PayPal",
        "refunded": ":leftwards_arrow_with_hook: Refunded",
        "refund_failed": ":x: Refund FAILED",
        "dispute": ":rotating_light: *PayPal dispute opened* — respond in PayPal",
        "cancelled": ":no_entry_sign: Booking cancelled",
        "void_failed": ":x: Could not release the card hold",
        "confirmed": ":page_facing_up: Final confirmation sent to customer",
        "fulfilled": ":checkered_flag: Rental fulfilled",
    }
    head = icons.get(kind, kind)
    extra = f" — {_esc(detail)}" if detail else ""
    await slack(f"{head}{extra}\n{line(r)}")


async def capture_run(captured: list, failed: list) -> None:
    if not captured and not failed:
        return
    total = sum((r.amount_captured or 0) for r in captured)
    parts = [f":moneybag: *Auto-capture:* {len(captured)} captured ({money(total)})"]
    if failed:
        parts[0] += f", *{len(failed)} failed*"
        parts += [f"• :x: {line(r)}" for r in failed[:10]]
    await slack("\n".join(parts))


# ---------------------------------------------------------------- daily digest

def _is_test(r) -> bool:
    email = (r.customer_email or "").lower()
    return (email.endswith(("example.com", "example.org", "example.net", "test.com",
                            "mailinator.com"))
            or (r.customer_name or "").strip().upper().startswith("TEST"))


def build_digest(rows: list, audits: dict[str, list], now: datetime | None = None,
                 capture_after_days: int = 3) -> str:
    """The daily Slack summary.

    rows   — reservations to consider (open ones + anything from the last 24h)
    audits — reservation id → its AuditLog rows
    """
    now = now or datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    rows = [r for r in rows if not _is_test(r)]

    def touched(r) -> bool:
        return any(a.agent_email not in SYSTEM_ACTORS for a in audits.get(r.id, []))

    def at(a) -> datetime:
        return _aware(a.created_at) or now

    open_ = [r for r in rows if r.status not in ("cancelled", "fulfilled")]
    new24 = [r for r in rows if (_aware(r.created_at) or now) >= day_ago]

    by_ps = lambda ps: [r for r in new24 if r.payment_status == ps]  # noqa: E731
    held = by_ps("authorized") + [r for r in new24 if r.payment_status == "captured"]
    declined, pending, unpaid = by_ps("failed"), by_ps("pending"), by_ps("none")

    events24 = [(r, a) for r in rows for a in audits.get(r.id, []) if at(a) >= day_ago]
    cap24 = {r.id: r for r, a in events24 if a.action in ("capture", "auto_capture",
                                                          "wh_capture_completed")}
    cancel24 = {r.id: r for r, a in events24 if a.action in ("cancel", "refund", "wh_refunded",
                                                             "wh_voided")}
    disputes = {r.id: r for r, a in events24 if a.action == "wh_dispute"}

    untouched = sorted([r for r in open_ if not touched(r)],
                       key=lambda r: _aware(r.created_at) or now)
    soon = []
    for r in open_:
        p = _pickup_dt(r)
        if p and now - timedelta(hours=6) <= p <= now + timedelta(hours=48) and not r.confirmation_sent:
            soon.append((p, r))
    soon.sort(key=lambda t: t[0])
    awaiting_conf = [r for r in open_ if r.payment_status in ("authorized", "captured")
                     and not r.confirmation_sent]
    stuck_pending = [r for r in open_ if r.payment_status == "pending"]
    cap_due = [r for r in open_ if r.payment_status == "authorized" and r.authorized_at
               and _aware(r.authorized_at) <= now + timedelta(hours=24)
               - timedelta(days=capture_after_days)]
    overdue_hold = [r for r in open_ if r.payment_status == "authorized" and r.authorized_at
                    and _aware(r.authorized_at) <= now - timedelta(days=capture_after_days, hours=6)]

    local = now.astimezone(_TZ)
    out = [f":bar_chart: *Daily summary — {local:%a %d %b}, {local:%H:%M} {_TZ_NAME}*"]

    out.append(
        f"*Last 24h:* {len(new24)} new reservation(s) — :white_check_mark: {len(held)} held "
        f"({money(sum(r.pay_now_total for r in held))}) · :x: {len(declined)} declined · "
        f":hourglass_flowing_sand: {len(pending)} in review · :new: {len(unpaid)} unpaid")
    out.append(
        f"*Money (24h):* :moneybag: {len(cap24)} captured "
        f"({money(sum(r.amount_captured or 0 for r in cap24.values()))}) · "
        f":no_entry_sign: {len(cancel24)} cancelled/refunded")

    def block(title: str, items: list, fmt=line, limit: int = 8) -> None:
        if not items:
            return
        out.append(f"\n{title} *({len(items)})*")
        out.extend(f"• {fmt(x)}" for x in items[:limit])
        if len(items) > limit:
            out.append(f"• …and {len(items) - limit} more in /admin")

    if disputes:
        block(":rotating_light: *Disputes opened — act now*", list(disputes.values()))
    block(":red_circle: *Untouched — no agent has worked these yet*", untouched,
          lambda r: f"{line(r)} · {r.payment_status} · waiting {_age(r.created_at, now)}")
    block(":rotating_light: *Pickup within 48h, final confirmation NOT sent*",
          [r for _, r in soon])
    block(":telephone_receiver: *Card declined (24h) — call back*", declined,
          lambda r: f"{line(r)} · {_esc(r.customer_phone) or 'no phone'}")
    block(":hourglass_flowing_sand: *Holds stuck in PayPal review*", stuck_pending)
    block(":warning: *Holds past the capture window — check why they weren't captured*",
          overdue_hold)

    out.append("")
    out.append(f":page_facing_up: Awaiting supplier confirmation: *{len(awaiting_conf)}*  ·  "
               f":credit_card: Auto-capture due in next 24h: *{len(cap_due)}* "
               f"({money(sum(r.amount_authorized or r.pay_now_total for r in cap_due))})  ·  "
               f":open_file_folder: Open bookings: *{len(open_)}*")
    if not (untouched or soon or declined or stuck_pending or overdue_hold or disputes):
        out.append(":white_check_mark: Nothing needs attention right now.")
    base = admin_base()
    if base:
        out.append(f"<{base}/admin/reservations|Open the admin portal>")
    return "\n".join(out)


async def send_digest(session) -> str:
    from sqlalchemy import or_, select

    from ..models_db import AuditLog, Reservation

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    rows = (await session.execute(select(Reservation).where(or_(
        Reservation.status.notin_(["cancelled", "fulfilled"]),
        Reservation.created_at >= day_ago,
        Reservation.id.in_(select(AuditLog.reservation_id).where(AuditLog.created_at >= day_ago)),
    )))).scalars().all()
    ids = [r.id for r in rows]
    audits: dict[str, list] = {}
    if ids:
        for a in (await session.execute(select(AuditLog).where(
                AuditLog.reservation_id.in_(ids)))).scalars().all():
            audits.setdefault(a.reservation_id, []).append(a)
    text = build_digest(rows, audits, now)
    await slack(text)
    return text
