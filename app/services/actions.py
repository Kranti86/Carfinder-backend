"""Booking actions shared by the /admin portal and Slack.

Every money/state change goes through `perform`, so both front-ends behave the same:
same checks, same AuditLog entries (with the acting agent), same customer emails and
the same Slack alert afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..models_db import AuditLog, Reservation
from . import email_service, notify, payments

MAX_DOC_BYTES = 10 * 1024 * 1024

ERRORS = {
    "no_capture": "Nothing has been captured, so there is nothing to refund.",
    "no_hold": "There is no card hold to release.",
    "void_failed": "Could not release the card hold, so the reservation was NOT cancelled. "
                   "Try again, or release it in PayPal.",
    "need_docs": "Add the supplier confirmation number AND attach the confirmation PDF first.",
    "confirm_first": "Send the final confirmation before marking it fulfilled.",
    "already_cancelled": "This booking is already cancelled.",
    "doc_too_big": "That file is over 10 MB.",
    "unknown_action": "Unknown action.",
}


@dataclass
class Result:
    ok: bool
    error: str = ""          # key in ERRORS
    message: str = ""        # human summary of what happened

    @property
    def error_text(self) -> str:
        return ERRORS.get(self.error, self.error)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def log(session, r, agent, action, detail=""):
    session.add(AuditLog(reservation_id=r.id, agent_id=getattr(agent, "id", None),
                         agent_email=getattr(agent, "email", "system"),
                         action=action, detail=detail))


async def release_hold(session, r, agent) -> bool:
    """Void the card hold. If PayPal refuses because nothing is held any more (the hold
    was DENIED, already VOIDED or EXPIRED), treat it as released. Returns True when no
    money is held afterwards."""
    res = await payments.void(r.payment_ref)
    if res.ok:
        r.payment_status = "voided"
        await log(session, r, agent, "void", f"card hold released {'(live)' if res.live else '(recorded)'}")
        return True
    status = await payments.authorization_status(r.payment_ref)
    if status in payments.RELEASED_STATUSES:
        r.payment_status = "voided" if status in ("VOIDED", "EXPIRED") else "failed"
        await log(session, r, agent, "void", f"nothing held: PayPal reports {status}")
        return True
    await log(session, r, agent, "void_failed", res.message)
    return False


async def attach_document(session, r, agent, blob: bytes, name: str) -> Result:
    if len(blob) > MAX_DOC_BYTES:
        return Result(False, "doc_too_big")
    r.confirmation_doc = blob
    r.confirmation_doc_name = (name or "confirmation.pdf")[:200]
    await log(session, r, agent, "upload_doc", r.confirmation_doc_name)
    await session.commit()
    return Result(True, message=f"Attached {r.confirmation_doc_name}")


def _final_payload(r) -> dict:
    return {
        "ref": r.id[:8], "customer_name": r.customer_name, "customer_email": r.customer_email,
        "supplier_reservation_number": r.supplier_reservation_number,
        "vehicle_name": r.vehicle_name, "vehicle_type": r.vehicle_type, "supplier": r.supplier,
        "city": r.city, "pickup_area": r.pickup_area, "pickup_datetime": r.pickup_datetime,
        "dropoff_datetime": r.dropoff_datetime, "base_price": r.base_price,
        "policy_number": r.policy_number,
    }


async def perform(session, r: Reservation, agent, action: str, value: str = "",
                  defer: Callable | None = None) -> Result:
    """Run one action on a reservation, commit, and post the Slack alert.

    `defer(fn, *args)` schedules slow side effects (customer emails) to run after the
    response; it defaults to running them inline.
    """
    defer = defer or (lambda fn, *a: fn(*a))
    who = getattr(agent, "name", "") or getattr(agent, "email", "system")
    value = (value or "").strip()
    alert: tuple[str, str] | None = None
    result = Result(True)

    if action == "capture":
        res = await payments.capture(r.payment_ref, r.amount_authorized or r.pay_now_total)
        if res.ok:
            r.payment_status = "captured"
            r.amount_captured = r.amount_authorized or r.pay_now_total
            r.capture_ref = res.ref            # PayPal capture id, needed for refunds
            r.status = "captured"
            await log(session, r, agent, "capture",
                      f"${r.amount_captured:.2f} {'(live)' if res.live else '(recorded)'}")
            alert = ("captured", f"${r.amount_captured:.2f} by {who}")
            result = Result(True, message=f"Captured ${r.amount_captured:.2f}")
        else:
            await log(session, r, agent, "capture_failed", res.message)
            alert = ("capture_failed", res.message)
            result = Result(False, message=f"Capture failed: {res.message}", error=res.message)

    elif action == "refund":
        if r.payment_status != "captured" or not r.capture_ref:
            return Result(False, "no_capture")
        res = await payments.refund(r.capture_ref, r.amount_captured or None)
        if res.ok:
            r.payment_status = "refunded"
            r.status = "cancelled"
            await log(session, r, agent, "refund",
                      "refunded; REMEMBER: clawback rentalcover net if fully cancelled")
            alert = ("refunded", f"by {who}")
            result = Result(True, message=f"Refunded ${r.amount_captured or 0:.2f}")
        else:
            await log(session, r, agent, "refund_failed", res.message)
            alert = ("refund_failed", res.message)
            result = Result(False, message=f"Refund failed: {res.message}", error=res.message)

    elif action == "void":
        if r.payment_status not in ("authorized", "pending") or not r.payment_ref:
            return Result(False, "no_hold")
        if await release_hold(session, r, agent):
            r.status = "cancelled"
            await log(session, r, agent, "cancel", "clawback rentalcover net if policy issued")
            alert = ("cancelled", f"hold released by {who}")
            result = Result(True, message="Card hold released and booking cancelled")
        else:
            await session.commit()
            await notify.payment_event(r, "void_failed", f"tried by {who}")
            return Result(False, "void_failed")

    elif action == "cancel":
        if r.status == "cancelled":
            return Result(False, "already_cancelled")
        payment_note = ""
        if r.payment_status in ("authorized", "pending") and r.payment_ref:
            # Release the hold now, so the daily auto-capture can never charge it.
            if not await release_hold(session, r, agent):
                await session.commit()
                await notify.payment_event(r, "void_failed", f"cancel tried by {who}")
                return Result(False, "void_failed")
            payment_note = "card hold released. "
        elif r.payment_status == "captured":
            payment_note = "payment already CAPTURED — use Refund if money is owed back. "
        r.status = "cancelled"
        await log(session, r, agent, "cancel",
                  (f"reason: {value}; " if value else "") + payment_note +
                  "customer emailed. Remember to clawback insurance if a policy was issued.")
        alert = ("cancelled", f"by {who}" + (f" — {value}" if value else ""))
        result = Result(True, message="Cancelled" + (" — card hold released" if payment_note.startswith("card") else "")
                        + (". Payment was already captured: refund if money is owed." if r.payment_status == "captured" else ""))
        defer(email_service.send_cancellation, {
            "ref": r.id[:8], "customer_name": r.customer_name,
            "customer_email": r.customer_email, "vehicle_name": r.vehicle_name,
            "city": r.city, "reason": value})

    elif action == "confirm_send":
        # Final supplier confirmation → email the customer the docs. Requires the
        # supplier confirmation number AND the attached confirmation PDF.
        if not r.supplier_reservation_number or not r.confirmation_doc:
            return Result(False, "need_docs")
        r.confirmation_sent = True
        r.confirmation_sent_at = _now()
        r.status = "confirmed"
        await log(session, r, agent, "final_confirmation_sent",
                  f"supplier #{r.supplier_reservation_number}, doc {r.confirmation_doc_name}")
        alert = ("confirmed", f"by {who}")
        result = Result(True, message=f"Final confirmation emailed to {r.customer_email}")
        defer(email_service.send_final_confirmation, _final_payload(r),
              bytes(r.confirmation_doc), r.confirmation_doc_name or "confirmation.pdf")

    elif action == "fulfill":
        # Locked until the final confirmation has been sent to the customer.
        if not r.confirmation_sent:
            return Result(False, "confirm_first")
        r.status = "fulfilled"
        await log(session, r, agent, "fulfill", "marked fulfilled + review invite sent")
        alert = ("fulfilled", f"by {who}")
        result = Result(True, message="Marked fulfilled; review invite sent")
        defer(email_service.send_review_invite, {
            "customer_name": r.customer_name, "customer_email": r.customer_email,
            "vehicle_name": r.vehicle_name, "city": r.city, "ref": r.id[:8]})

    elif action == "supplier_ref":
        r.supplier_reservation_number = value
        await log(session, r, agent, "supplier_ref", f"set to {value}")
        result = Result(True, message=f"Supplier ref set to {value}")

    elif action == "policy":
        r.policy_number = value
        await log(session, r, agent, "policy", f"policy {value}")
        result = Result(True, message=f"Policy number set to {value}")

    elif action == "note":
        await log(session, r, agent, "note", value)
        result = Result(True, message="Note added")

    elif action == "claim":
        await log(session, r, agent, "claim", f"claimed by {who}")
        result = Result(True, message=f"Claimed by {who}")

    else:
        return Result(False, "unknown_action")

    await session.commit()
    if alert:
        await notify.payment_event(r, *alert)
    return result
