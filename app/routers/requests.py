"""Online checkout: quotes, PayPal orders, reservations, and lookup.

Every amount is decided HERE, from the server-side quote stored when the search
finished (app/services/quotes.py). The browser only says which car
(job_id + vehicle_id) and whether it wants insurance.

  POST /quote               → authoritative price breakdown for one car
  POST /paypal/create-order → PayPal AUTHORIZE order for the server-computed amount,
                              pre-assigning the reservation id (PayPal custom_id)
  POST /requests            → persist the reservation (persist-first), then place the
                              auth hold. Idempotent per PayPal order.
  POST /lookup              → find a reservation by full reference + email/last name

Capture happens after 3 days (app/cli.py capture-due) or from the portal.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_session
from ..guard import is_verified
from ..models_db import AuditLog, Reservation
from ..rate_limit import check_rate_limit
from ..services import email_service, payments, quotes, settings_service

logger = logging.getLogger("carfinder.requests")
router = APIRouter(tags=["reservations"])

_REF_RE = re.compile(r"[0-9a-f]{8}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def _gate(request: Request, settings: Settings, bucket: str, limit: int,
                window: int) -> JSONResponse | None:
    """Origin guard + per-client rate limit for the money/PII endpoints."""
    if not is_verified(request, settings):
        return _err("forbidden", "Not allowed from this origin.", 403)
    if not await check_rate_limit(request, bucket=bucket, limit=limit, window=window):
        return _err("rate_limited", "Too many attempts — please wait a few minutes.", 429)
    return None


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


async def _notify_slack(text: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(url, json={"text": text})
    except Exception as exc:  # never let a notification failure break a booking
        logger.warning("slack notify failed: %s", exc)


def _clean_customer(raw) -> dict | None:
    raw = raw if isinstance(raw, dict) else {}
    c = {
        "name": str(raw.get("name") or "").strip()[:160],
        "email": str(raw.get("email") or "").strip().lower()[:255],
        "phone": str(raw.get("phone") or "").strip()[:40],
    }
    if not c["name"] or not _EMAIL_RE.fullmatch(c["email"]) or not c["phone"]:
        return None
    return c


def _margin(r: Reservation) -> float:
    ins_commission = max(0.0, (r.insurance_price or 0.0) - (r.insurance_net or 0.0))
    return round((r.service_fee or 0.0) + ins_commission, 2)


@router.get("/online-config")
async def online_config() -> dict:
    """Everything the online funnel needs on load: the service-fee toggle, PayPal SDK
    config, and the insurance offer (display only — /quote decides the amount)."""
    s = get_settings()
    return {
        "service_fee_enabled": await settings_service.service_fee_enabled(),
        "currency": "USD",
        "insurance": {
            "enabled": s.insurance_enabled,
            "label": s.insurance_label,
            "price_per_day": quotes.insurance_price_per_day(),
        },
        "paypal": {
            "clientId": os.environ.get("PAYPAL_CLIENT_ID", ""),
            "env": os.environ.get("PAYPAL_ENV", "sandbox"),
            "configured": payments.is_configured(),
        },
    }


@router.post("/quote")
async def quote(request: Request):
    body = await _body(request)
    q = await quotes.compute(str(body.get("job_id") or ""), str(body.get("vehicle_id") or ""),
                             insurance=False)
    if not q:
        return _err("quote_expired", "These prices have expired — please search again.", 409)
    return quotes.public_quote(q)


@router.post("/paypal/create-order")
async def paypal_create_order(request: Request,
                              settings: Settings = Depends(get_settings)):
    """Create an AUTHORIZE-intent order for the SERVER-computed amount. The reservation
    id is assigned now and stamped on the order as custom_id, so webhooks can always
    find the reservation. Any client-sent amount is ignored."""
    blocked = await _gate(request, settings, "order", settings.order_rate_limit,
                          settings.order_rate_window)
    if blocked:
        return blocked
    body = await _body(request)
    job_id = str(body.get("job_id") or "")
    vehicle_id = str(body.get("vehicle_id") or "")
    insurance = bool(body.get("insurance"))
    q = await quotes.compute(job_id, vehicle_id, insurance)
    if not q:
        return _err("quote_expired", "These prices have expired — please search again.", 409)
    if q["pay_now"] <= 0:
        return _err("nothing_to_pay", "Nothing is due online for this reservation.", 400)

    reservation_id = uuid.uuid4().hex
    res = await payments.create_order(q["pay_now"], reservation_id,
                                      customer=_clean_customer(body.get("customer")) or {})
    if not res.ok or not res.ref:
        logger.warning("create_order failed: %s", res.message)
        return _err("payment_unavailable", "Card payment is unavailable right now.", 502)
    await quotes.remember_order(res.ref, reservation_id=reservation_id, job_id=job_id,
                                vehicle_id=vehicle_id, insurance=insurance, amount=q["pay_now"])
    return {"ok": True, "order_id": res.ref, "amount": q["pay_now"],
            "configured": payments.is_configured()}


def _reservation_public(r: Reservation) -> dict:
    """Customer-safe subset of a reservation (no internal payment refs)."""
    return {
        "id": r.id, "ref": r.id[:8].upper(), "status": r.status,
        "payment_status": r.payment_status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "customer_name": r.customer_name, "customer_email": r.customer_email,
        "city": r.city, "pickup_area": r.pickup_area,
        "pickup_datetime": r.pickup_datetime, "dropoff_datetime": r.dropoff_datetime,
        "vehicle_name": r.vehicle_name, "vehicle_type": r.vehicle_type, "supplier": r.supplier,
        "currency": r.currency, "base_price": r.base_price, "service_fee": r.service_fee,
        "insurance_price": r.insurance_price, "pay_now": r.pay_now_total,
        "supplier_reservation_number": r.supplier_reservation_number,
        "policy_number": r.policy_number,
    }


def _last_name(full: str) -> str:
    parts = (full or "").strip().lower().split()
    return parts[-1] if parts else ""


@router.post("/lookup")
async def lookup(request: Request, session: AsyncSession = Depends(get_session),
                 settings: Settings = Depends(get_settings)):
    """Find a past reservation. Needs the FULL 8-character reservation number AND an exact
    email or last-name match. No wildcards, rate-limited, so it can't be used to page
    through other customers' bookings."""
    if not await check_rate_limit(request, bucket="lookup", limit=settings.lookup_rate_limit,
                                  window=settings.lookup_rate_window):
        return _err("rate_limited", "Too many attempts — please wait a few minutes.", 429)
    body = await _body(request)
    ref = str(body.get("ref") or "").strip().replace("#", "").lower()
    email = str(body.get("email") or "").strip().lower()
    last = str(body.get("last_name") or "").strip().lower()
    if not _REF_RE.fullmatch(ref) or not (email or last):
        return _err("bad_request", "Enter your 8-character reservation number and the email "
                                   "or last name you booked with.", 400)
    # ref is validated hex, so it is safe as a LIKE prefix.
    candidates = (await session.execute(
        select(Reservation).where(Reservation.id.like(f"{ref}%")).limit(5))).scalars().all()
    for r in candidates:
        if email and r.customer_email == email:
            return _reservation_public(r)
        if not email and last and _last_name(r.customer_name) == last:
            return _reservation_public(r)
    return _err("not_found", "No reservation found with those details.", 404)


@router.post("/requests")
async def create_request(request: Request, background: BackgroundTasks,
                         session: AsyncSession = Depends(get_session),
                         settings: Settings = Depends(get_settings)):
    blocked = await _gate(request, settings, "requests", settings.requests_rate_limit,
                          settings.requests_rate_window)
    if blocked:
        return blocked
    payload = await _body(request)

    customer = _clean_customer(payload.get("customer"))
    if not customer:
        return _err("bad_customer", "Please enter a name, a valid email and a phone number.", 400)

    order_id = str(payload.get("paypal_order_id") or "")
    order = None
    if order_id:
        order = await quotes.get_order(order_id)
        if not order:
            # Not an order we created (or it expired). Nothing has been held.
            return _err("payment_expired", "Your payment session expired — no charge was "
                                           "made. Please try again.", 409)
        # Idempotent: a retry after a lost response returns the same reservation.
        existing = await session.get(Reservation, order["reservation_id"])
        if existing:
            return {**_reservation_public(existing), "conversion_value": _margin(existing),
                    "duplicate": True}
        job_id, vehicle_id, insurance = order["job_id"], order["vehicle_id"], order["insurance"]
    else:
        job_id = str(payload.get("job_id") or "")
        vehicle_id = str(payload.get("vehicle_id") or "")
        insurance = bool(payload.get("insurance"))

    q = await quotes.compute(job_id, vehicle_id, insurance)
    if not q:
        return _err("quote_expired", "These prices have expired — please search again.", 409)
    if order and abs(q["pay_now"] - float(order["amount"])) > 0.005:
        return _err("price_changed", "The price changed since you started checkout — no "
                                     "charge was made. Please review and try again.", 409)

    vehicle, search, ins = q["vehicle"], q["search"], q["insurance"]
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    r = Reservation(
        id=order["reservation_id"] if order else uuid.uuid4().hex,
        source=str(payload.get("funnel") or "online")[:20],
        customer_name=customer["name"],
        customer_email=customer["email"],
        customer_phone=customer["phone"],
        city=(search.get("label") or "")[:120],
        pickup_area=(vehicle.get("pickup_location") or "")[:200],
        pickup_datetime=search.get("pickup", ""),
        dropoff_datetime=search.get("dropoff", ""),
        vehicle_name=(vehicle.get("name") or "")[:160],
        vehicle_type=(vehicle.get("vehicle_type") or "")[:60],
        supplier=(vehicle.get("supplier") or "")[:120],
        currency=q["currency"],
        base_price=q["base_price"],
        service_fee=q["service_fee"],
        insurance_price=ins["price"] if ins else 0.0,
        insurance_net=ins["net"] if ins else 0.0,
        paypal_order_id=order_id,
        gclid=str(source.get("gclid") or "")[:255],
        ads_opt_out=str(source.get("consent") or "").lower() == "denied",
        utm_campaign=str(source.get("utm") or "")[:255],
        status="new",
        payment_status="none",
    )
    session.add(r)
    await session.flush()
    session.add(AuditLog(reservation_id=r.id, agent_email="system", action="created",
                         detail=f"source={r.source} quote={job_id}/{vehicle_id}"))

    amount = r.pay_now_total
    if order_id and amount > 0:
        res = await payments.authorize(amount, order_id, reservation_id=r.id)
        if res.ok and res.amount is not None and abs(res.amount - amount) > 0.005:
            # Held amount doesn't match what we priced: release it, don't keep it.
            await payments.void(res.ref)
            session.add(AuditLog(reservation_id=r.id, agent_email="system",
                                 action="authorize_mismatch",
                                 detail=f"held ${res.amount:.2f} != expected ${amount:.2f}; voided"))
            r.payment_status = "failed"
        elif res.ok and res.status in payments.PENDING_STATUSES:
            # PayPal is reviewing the hold: not guaranteed yet. The webhook flips it to
            # authorized when PayPal approves it.
            r.payment_status = "pending"
            r.payment_ref = res.ref
            session.add(AuditLog(reservation_id=r.id, agent_email="system",
                                 action="authorize_pending",
                                 detail=f"${amount:.2f} hold under PayPal review ref={res.ref}"))
        elif res.ok:
            r.payment_status = "authorized"
            r.payment_ref = res.ref
            r.amount_authorized = amount
            r.authorized_at = datetime.now(timezone.utc)
            r.status = "authorized"
            session.add(AuditLog(reservation_id=r.id, agent_email="system",
                                 action="authorize",
                                 detail=f"${amount:.2f} hold {'(live)' if res.live else '(recorded)'} ref={res.ref}"))
        else:
            r.payment_status = "failed"
            if res.ref:
                r.payment_ref = res.ref
            session.add(AuditLog(reservation_id=r.id, agent_email="system",
                                 action="authorize_failed", detail=res.message))

    await session.commit()

    await _notify_slack(
        f":car: New reservation {r.id[:8]} — {r.customer_name or 'guest'} · "
        f"{r.vehicle_name or '?'} in {r.city or '?'} · pay-now ${amount:.2f} · "
        f"{r.status}/{r.payment_status}")

    data = _reservation_public(r)
    background.add_task(email_service.send_confirmation, data)
    return {**data, "conversion_value": _margin(r)}
