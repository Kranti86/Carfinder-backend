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
from ..services import actions, audiences, conversions, email_service, notify, payments, settings_service

logger = logging.getLogger("carfinder.admin")

router = APIRouter(tags=["admin"], prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates_admin"))


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


_release_hold = actions.release_hold   # kept for older imports
_log = actions.log


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
    errs = {**actions.ERRORS, "need_docs": "Attach the supplier confirmation PDF and enter the supplier "
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
    res = await actions.attach_document(session, r, agent, await file.read(), file.filename or "")
    if not res.ok:
        return RedirectResponse(f"/admin/reservations/{rid}?err=need_docs", status_code=303)
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
    res = await actions.perform(session, r, agent, action, value, defer=background.add_task)
    if not res.ok and res.error in actions.ERRORS:
        return RedirectResponse(f"/admin/reservations/{rid}?err={res.error}", status_code=303)
    return RedirectResponse(f"/admin/reservations/{rid}", status_code=303)
