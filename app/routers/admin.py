"""Server-rendered ops portal at /admin (per-agent login, deal-funnel view, actions).

Every money/state action is written to the AuditLog with the acting agent's email,
so you always know who captured, refunded, cancelled, or edited a reservation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (COOKIE_NAME, SESSION_MAX_AGE, agent_from_request, authenticate,
                    make_session)
import logging

from ..config import get_settings
from ..db import get_session
from ..rate_limit import check_rate_limit
from ..models_db import STATUS_FLOW, AuditLog, Reservation
from ..services import audiences, conversions, email_service, notify, payments, settings_service

logger = logging.getLogger("carfinder.admin")

router = APIRouter(tags=["admin"], prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates_admin"))


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


async def _release_hold(session, r, agent) -> bool:
    """Void the card hold. If PayPal refuses because nothing is held any more (the hold
    was DENIED, already VOIDED or EXPIRED), treat it as released. Returns True when no
    money is held afterwards."""
    res = await payments.void(r.payment_ref)
    if res.ok:
        r.payment_status = "voided"
        await _log(session, r, agent, "void", f"card hold released {'(live)' if res.live else '(recorded)'}")
        return True
    status = await payments.authorization_status(r.payment_ref)
    if status in payments.RELEASED_STATUSES:
        r.payment_status = "voided" if status in ("VOIDED", "EXPIRED") else "failed"
        await _log(session, r, agent, "void", f"nothing held: PayPal reports {status}")
        return True
    await _log(session, r, agent, "void_failed", res.message)
    return False


async def _log(session, r, agent, action, detail=""):
    session.add(AuditLog(reservation_id=r.id, agent_id=getattr(agent, "id", None),
                         agent_email=getattr(agent, "email", "system"),
                         action=action, detail=detail))


# --------------------------------------------------------------------------- #
# auth pages
# --------------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": ""})


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...),
                       session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    if not await check_rate_limit(request, bucket="admin-login", limit=settings.login_rate_limit,
                                  window=settings.login_rate_window):
        logging.getLogger("carfinder.admin").warning("admin login rate-limited")
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Too many attempts. Wait 15 minutes and try again."},
            status_code=429)
    agent = await authenticate(session, email, password)
    if not agent:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Invalid email or password."}, status_code=401)
    resp = RedirectResponse("/admin/reservations", status_code=303)
    resp.set_cookie(COOKIE_NAME, make_session(agent.id), max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="lax", secure=True)
    return resp


@router.get("/logout")
async def logout():
    resp = _login_redirect()
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("")
async def admin_root():
    return RedirectResponse("/admin/reservations", status_code=303)


# --------------------------------------------------------------------------- #
# settings (admin only): the service-fee toggle
# --------------------------------------------------------------------------- #
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    if agent.role != "admin":
        return HTMLResponse("Admins only.", status_code=403)
    fee_on = await settings_service.service_fee_enabled()
    return templates.TemplateResponse(request=request, name="settings.html",
                                      context={"agent": agent, "fee_on": fee_on})


@router.post("/settings")
async def settings_save(request: Request, enabled: str = Form(""),
                        session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    if agent.role != "admin":
        return HTMLResponse("Admins only.", status_code=403)
    val = "true" if enabled == "on" else "false"
    await settings_service.set("service_fee_enabled", val)
    logger.info("service_fee_enabled set to %s by %s", val, agent.email)
    return RedirectResponse("/admin/settings", status_code=303)


# --------------------------------------------------------------------------- #
# Google Ads offline conversions — one-click CSV downloads (any logged-in agent)
# --------------------------------------------------------------------------- #
def _csv_response(text: str, filename: str) -> Response:
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/conversions.csv")
async def download_conversions(request: Request, session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    name = get_settings().conversions_action_name or conversions.DEFAULT_CONVERSION_NAME
    text, _ = await conversions.conversions_csv(session, name)
    return _csv_response(text, "ads_conversions.csv")


@router.get("/conversions-retractions.csv")
async def download_retractions(request: Request, session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    name = get_settings().conversions_action_name or conversions.DEFAULT_CONVERSION_NAME
    text, _ = await conversions.retractions_csv(session, name)
    return _csv_response(text, "ads_conversion_retractions.csv")


@router.get("/audience/{segment}.csv")
async def download_audience(segment: str, request: Request,
                            session: AsyncSession = Depends(get_session)):
    """One-click Customer Match list (hashed) for Google Ads → Audience manager."""
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    if segment not in audiences.SEGMENTS:
        return HTMLResponse("Unknown segment", status_code=404)
    text, _ = await audiences.audience_csv(session, segment)
    return _csv_response(text, f"audience_{segment}.csv")


@router.get("/exports", response_class=HTMLResponse)
async def exports_page(request: Request, session: AsyncSession = Depends(get_session)):
    """All data downloads in one place, with live counts per audience segment."""
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    counts = {seg: len(await audiences.collect(session, seg)) for seg in audiences.SEGMENTS}
    rows = "".join(
        f'<tr><td><b>{seg}</b></td><td>{desc}</td><td style="text-align:right">{counts[seg]}</td>'
        f'<td><a href="/admin/audience/{seg}.csv">Download CSV</a></td></tr>'
        for seg, desc in audiences.SEGMENTS.items())
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Exports</title>
<style>body{{font-family:system-ui,Segoe UI,Arial;margin:30px;color:#1e2633}}table{{border-collapse:collapse;margin:10px 0 24px}}
td,th{{border-bottom:1px solid #e6ebf3;padding:9px 12px;text-align:left;font-size:14px}}a{{color:#1667c1}}
.note{{background:#fff8e6;border:1px solid #f0d78a;border-radius:8px;padding:10px 12px;font-size:13px;max-width:820px}}</style></head>
<body><p><a href="/admin/reservations">&larr; Reservations</a></p>
<h2>Google Ads audiences (Customer Match)</h2>
<p class="note">Hashed (SHA-256) email + phone, in Google's format. Upload in Google Ads &rarr; Tools &rarr;
Audience manager &rarr; + &rarr; Customer list &rarr; <b>Upload hashed data</b>; refresh with
<b>Replace all data</b>. Visitors who rejected cookies and test bookings are always left out.
Google needs about 100 matched people before a list can be used.</p>
<table><tr><th>Segment</th><th>Who</th><th>People</th><th></th></tr>{rows}</table>
<h2>Google Ads offline conversions</h2>
<p><a href="/admin/conversions.csv">Confirmed bookings CSV</a> &middot;
<a href="/admin/conversions-retractions.csv">Retractions CSV</a>
(also fetched automatically by the scheduled uploads)</p></body></html>"""
    return HTMLResponse(html)


# --------------------------------------------------------------------------- #
# reservations
# --------------------------------------------------------------------------- #
@router.get("/reservations", response_class=HTMLResponse)
async def reservations_list(request: Request, status: str = "", q: str = "",
                            session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()

    stmt = select(Reservation).order_by(Reservation.created_at.desc())
    if status:
        stmt = stmt.where(Reservation.status == status)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            (Reservation.customer_email.ilike(like))
            | (Reservation.customer_name.ilike(like))
            | (Reservation.city.ilike(like))
            | (Reservation.id.ilike(like)))
    rows = (await session.execute(stmt.limit(300))).scalars().all()

    grouped = dict((await session.execute(
        select(Reservation.status, func.count()).group_by(Reservation.status))).all())
    counts = {s: grouped.get(s, 0) for s in STATUS_FLOW}

    return templates.TemplateResponse(request=request, name="list.html", context={
        "agent": agent, "rows": rows, "statuses": STATUS_FLOW,
        "active_status": status, "q": q, "counts": counts})


@router.get("/reservations/{rid}", response_class=HTMLResponse)
async def reservation_detail(request: Request, rid: str,
                             session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    r = await session.get(Reservation, rid)
    if not r:
        return HTMLResponse("Reservation not found", status_code=404)
    events = (await session.execute(
        select(AuditLog).where(AuditLog.reservation_id == rid)
        .order_by(AuditLog.created_at.desc()))).scalars().all()
    errs = {"need_docs": "Attach the supplier confirmation PDF and enter the supplier "
                         "confirmation number before sending the final confirmation.",
            "confirm_first": "Send the final confirmation to the customer before marking fulfilled.",
            "void_failed": "Could not release the card hold, so the reservation was NOT cancelled. "
                           "Check the audit log and retry, or void it in PayPal.",
            "no_capture": "There is no captured payment to refund.",
            "no_hold": "There is no active card hold to void."}
    return templates.TemplateResponse(request=request, name="detail.html", context={
        "agent": agent, "r": r, "events": events,
        "payments_live": payments.is_configured(),
        "err": errs.get(request.query_params.get("err", ""), "")})


@router.post("/reservations/{rid}/upload")
async def upload_document(request: Request, rid: str, file: UploadFile = File(...),
                         session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    r = await session.get(Reservation, rid)
    if not r:
        return HTMLResponse("Reservation not found", status_code=404)
    blob = await file.read()
    if len(blob) > 10 * 1024 * 1024:
        return RedirectResponse(f"/admin/reservations/{rid}?err=need_docs", status_code=303)
    r.confirmation_doc = blob
    r.confirmation_doc_name = (file.filename or "confirmation.pdf")[:200]
    await _log(session, r, agent, "upload_doc", r.confirmation_doc_name)
    await session.commit()
    if alert:
        await notify.payment_event(r, *alert)
    return RedirectResponse(f"/admin/reservations/{rid}", status_code=303)


@router.get("/reservations/{rid}/document")
async def get_document(request: Request, rid: str,
                       session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    r = await session.get(Reservation, rid)
    if not r or not r.confirmation_doc:
        return HTMLResponse("No document", status_code=404)
    name = r.confirmation_doc_name or "confirmation.pdf"
    media = "application/pdf" if name.lower().endswith(".pdf") else "application/octet-stream"
    return Response(bytes(r.confirmation_doc), media_type=media,
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.post("/reservations/{rid}/action")
async def reservation_action(request: Request, rid: str, background: BackgroundTasks,
                             action: str = Form(...), value: str = Form(""),
                             session: AsyncSession = Depends(get_session)):
    agent = await agent_from_request(request, session)
    if not agent:
        return _login_redirect()
    r = await session.get(Reservation, rid)
    if not r:
        return HTMLResponse("Reservation not found", status_code=404)

    alert: tuple[str, str] | None = None
    who = agent.name or agent.email

    if action == "capture":
        res = await payments.capture(r.payment_ref, r.amount_authorized or r.pay_now_total)
        if res.ok:
            r.payment_status = "captured"
            r.amount_captured = r.amount_authorized or r.pay_now_total
            r.capture_ref = res.ref            # PayPal capture id, needed for refunds
            r.status = "captured"
            await _log(session, r, agent, "capture",
                       f"${r.amount_captured:.2f} {'(live)' if res.live else '(recorded)'}")
            alert = ("captured", f"${r.amount_captured:.2f} by {who}")
        else:
            await _log(session, r, agent, "capture_failed", res.message)
            alert = ("capture_failed", res.message)

    elif action == "refund":
        if r.payment_status != "captured" or not r.capture_ref:
            return RedirectResponse(f"/admin/reservations/{rid}?err=no_capture", status_code=303)
        res = await payments.refund(r.capture_ref, r.amount_captured or None)
        if res.ok:
            r.payment_status = "refunded"
            r.status = "cancelled"
            await _log(session, r, agent, "refund",
                       "refunded; REMEMBER: clawback rentalcover net if fully cancelled")
            alert = ("refunded", f"by {who}")
        else:
            await _log(session, r, agent, "refund_failed", res.message)
            alert = ("refund_failed", res.message)

    elif action == "void":
        if r.payment_status not in ("authorized", "pending") or not r.payment_ref:
            return RedirectResponse(f"/admin/reservations/{rid}?err=no_hold", status_code=303)
        if await _release_hold(session, r, agent):
            r.status = "cancelled"
            await _log(session, r, agent, "cancel",
                       "clawback rentalcover net if policy issued")
            alert = ("cancelled", f"hold released by {who}")
        else:
            await session.commit()
            await notify.payment_event(r, "void_failed", f"tried by {who}")
            return RedirectResponse(f"/admin/reservations/{rid}?err=void_failed", status_code=303)

    elif action == "cancel":
        reason = value.strip()
        payment_note = ""
        if r.payment_status in ("authorized", "pending") and r.payment_ref:
            # Release the hold now, so the daily auto-capture can never charge it.
            if not await _release_hold(session, r, agent):
                await session.commit()
                await notify.payment_event(r, "void_failed", f"cancel tried by {who}")
                return RedirectResponse(f"/admin/reservations/{rid}?err=void_failed",
                                        status_code=303)
            payment_note = "card hold released. "
        elif r.payment_status == "captured":
            payment_note = "payment already CAPTURED — use Refund if money is owed back. "
        r.status = "cancelled"
        await _log(session, r, agent, "cancel",
                   (f"reason: {reason}; " if reason else "") + payment_note +
                   "customer emailed. Remember to clawback insurance if a policy was issued.")
        alert = ("cancelled", f"by {who}" + (f" — {reason}" if reason else ""))
        background.add_task(email_service.send_cancellation, {
            "ref": r.id[:8], "customer_name": r.customer_name,
            "customer_email": r.customer_email, "vehicle_name": r.vehicle_name,
            "city": r.city, "reason": reason})

    elif action == "confirm_send":
        # Final supplier confirmation → email the customer the docs. Requires the
        # supplier confirmation number AND the attached confirmation PDF.
        if not r.supplier_reservation_number or not r.confirmation_doc:
            return RedirectResponse(f"/admin/reservations/{rid}?err=need_docs", status_code=303)
        r.confirmation_sent = True
        r.confirmation_sent_at = datetime.now(timezone.utc)
        r.status = "confirmed"
        await _log(session, r, agent, "final_confirmation_sent",
                   f"supplier #{r.supplier_reservation_number}, doc {r.confirmation_doc_name}")
        alert = ("confirmed", f"by {who}")
        background.add_task(email_service.send_final_confirmation, {
            "ref": r.id[:8], "customer_name": r.customer_name, "customer_email": r.customer_email,
            "supplier_reservation_number": r.supplier_reservation_number,
            "vehicle_name": r.vehicle_name, "vehicle_type": r.vehicle_type, "supplier": r.supplier,
            "city": r.city, "pickup_area": r.pickup_area, "pickup_datetime": r.pickup_datetime,
            "dropoff_datetime": r.dropoff_datetime, "base_price": r.base_price,
            "policy_number": r.policy_number,
        }, bytes(r.confirmation_doc), r.confirmation_doc_name or "confirmation.pdf")

    elif action == "fulfill":
        # Locked until the final confirmation has been sent to the customer.
        if not r.confirmation_sent:
            return RedirectResponse(f"/admin/reservations/{rid}?err=confirm_first", status_code=303)
        r.status = "fulfilled"
        await _log(session, r, agent, "fulfill", "marked fulfilled + review invite sent")
        alert = ("fulfilled", f"by {who}")
        background.add_task(email_service.send_review_invite, {
            "customer_name": r.customer_name, "customer_email": r.customer_email,
            "vehicle_name": r.vehicle_name, "city": r.city, "ref": r.id[:8]})

    elif action == "supplier_ref":
        r.supplier_reservation_number = value.strip()
        await _log(session, r, agent, "supplier_ref", f"set to {value.strip()}")

    elif action == "policy":
        r.policy_number = value.strip()
        await _log(session, r, agent, "policy", f"policy {value.strip()}")

    elif action == "note":
        await _log(session, r, agent, "note", value.strip())

    await session.commit()
    if alert:
        await notify.payment_event(r, *alert)
    return RedirectResponse(f"/admin/reservations/{rid}", status_code=303)
