"""PayPal webhook receiver — keeps reservations in sync with events we don't initiate.

Handles: authorization created/voided, capture completed/denied/pending/refunded, and
disputes. Every event's signature is verified with PayPal; events that cannot be
verified (no PAYPAL_WEBHOOK_ID) are rejected. Always returns 200 once
accepted so PayPal doesn't retry-storm; unknown events are acknowledged and ignored.

Set the webhook URL in the PayPal dashboard to:
    https://<your-backend>/paypal/webhook
and put the resulting Webhook ID in the PAYPAL_WEBHOOK_ID config var.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models_db import AuditLog, Reservation
from ..services import payments, notify

logger = logging.getLogger("carfinder.webhooks")
router = APIRouter(tags=["webhooks"])


def _ids(resource: dict) -> dict:
    """Every PayPal id an event can point back to us with."""
    related = ((resource.get("supplementary_data") or {}).get("related_ids") or {})
    up = [str(l.get("href", "")).rstrip("/").rsplit("/", 1)[-1]
          for l in (resource.get("links") or []) if l.get("rel") == "up" and l.get("href")]
    return {
        "custom_id": resource.get("custom_id") or "",
        "order_id": related.get("order_id") or "",
        # an event's own id, its parent ("up" link: capture → authorization,
        # refund → capture), and any related ids PayPal includes
        "payment_ids": [i for i in [resource.get("id"), related.get("authorization_id"),
                                    related.get("capture_id"), *up] if i],
    }


async def _find_reservation(session: AsyncSession, resource: dict) -> Reservation | None:
    ids = _ids(resource)
    # custom_id = our reservation id (stamped on the order at /paypal/create-order)
    if ids["custom_id"]:
        r = await session.get(Reservation, ids["custom_id"])
        if r:
            return r
    if ids["order_id"]:
        r = (await session.execute(select(Reservation).where(
            Reservation.paypal_order_id == ids["order_id"]).limit(1))).scalar_one_or_none()
        if r:
            return r
    if ids["payment_ids"]:
        return (await session.execute(
            select(Reservation).where(
                or_(Reservation.payment_ref.in_(ids["payment_ids"]),
                    Reservation.capture_ref.in_(ids["payment_ids"])))
            .limit(1))).scalar_one_or_none()
    return None


@router.post("/paypal/webhook")
async def paypal_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    event = await request.json()

    verified = await payments.verify_webhook(request.headers, event)
    if not verified:
        if not payments.webhook_verification_enabled():
            logger.error("paypal webhook REJECTED: cannot verify — set PAYPAL_CLIENT_ID, "
                         "PAYPAL_CLIENT_SECRET and PAYPAL_WEBHOOK_ID")
        else:
            logger.warning("paypal webhook signature verification FAILED")
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    etype = event.get("event_type", "")
    resource = event.get("resource") or {}
    r = await _find_reservation(session, resource)
    if not r:
        logger.info("webhook %s: no matching reservation", etype)
        return {"ok": True, "matched": False}

    prev_ps = r.payment_status
    alert: tuple[str, str] | None = None

    def log(action, detail=""):
        session.add(AuditLog(reservation_id=r.id, agent_email="paypal-webhook",
                             action=action, detail=detail))

    if etype == "PAYMENT.AUTHORIZATION.CREATED":
        # Only an approved hold (status CREATED) counts — never flip a declined one.
        if (str(resource.get("status", "CREATED")).upper() == "CREATED"
                and r.payment_status in ("none", "failed", "pending")):
            r.payment_status = "authorized"
            r.payment_ref = resource.get("id", r.payment_ref)
            if r.status == "new":
                r.status = "authorized"
            if prev_ps in ("pending", "failed"):
                alert = ("approved", "")
        log("wh_authorized", resource.get("id", ""))

    elif etype == "PAYMENT.AUTHORIZATION.VOIDED":
        r.payment_status = "voided"
        r.status = "cancelled"
        log("wh_voided", "authorization voided by PayPal (expired or manual)")
        if prev_ps != "voided":
            alert = ("voided", "by PayPal")

    elif etype == "PAYMENT.CAPTURE.COMPLETED":
        r.payment_status = "captured"
        r.capture_ref = resource.get("id", r.capture_ref)
        r.status = "captured"
        amt = (resource.get("amount") or {}).get("value")
        if amt:
            try:
                r.amount_captured = float(amt)
            except ValueError:
                pass
        log("wh_capture_completed", f"capture {resource.get('id','')}")
        if prev_ps != "captured":
            alert = ("captured", money_of(resource))

    elif etype == "PAYMENT.CAPTURE.PENDING":
        r.payment_status = "pending"
        log("wh_capture_pending", "capture under review")
        if prev_ps != "pending":
            alert = ("capture_pending", "")

    elif etype == "PAYMENT.CAPTURE.DENIED":
        r.payment_status = "failed"
        log("wh_capture_denied", "capture denied by PayPal")
        alert = ("capture_denied", "")

    elif etype in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
        r.payment_status = "refunded"
        r.status = "cancelled"
        log("wh_refunded", "refund/reversal — clawback rentalcover net if applicable")
        if prev_ps != "refunded":
            alert = ("refunded", "reversal/chargeback" if etype.endswith("REVERSED") else "via PayPal")

    elif etype.startswith("CUSTOMER.DISPUTE"):
        log("wh_dispute", f"{etype} — {resource.get('dispute_id','')}: review in PayPal")
        if etype == "CUSTOMER.DISPUTE.CREATED":
            alert = ("dispute", resource.get("reason", "") or "")

    else:
        log("wh_ignored", etype)

    await session.commit()
    if alert:
        await notify.payment_event(r, *alert)
    return {"ok": True, "matched": True, "event": etype}


def money_of(resource: dict) -> str:
    amt = (resource.get("amount") or {}).get("value")
    return f"${amt}" if amt else ""
