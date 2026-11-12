"""Run the business from Slack: the /rentals slash command and the buttons/modals on
booking messages.

Slack setup (one app, "Therentalcars Slack Notifier"):
  * Interactivity request URL  → https://<backend>/slack/interact
  * Slash command /rentals     → https://<backend>/slack/command
  * Bot scopes: commands, files:read  (files:read lets us fetch the PDF you attach)
Env on the backend:
  * SLACK_SIGNING_SECRET  — every request from Slack is checked against it
  * SLACK_BOT_TOKEN       — xoxb-… (opens forms, downloads the attached PDF)
  * SLACK_ADMINS          — who may act: "U0123ABC:you@yourdomain.com" (comma-separated).
                            The email must be an active /admin login; actions are logged
                            under it. Nobody else can see bookings or press buttons.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select

from ..auth import hash_password
from ..db import session_factory
from ..models_db import Agent, AuditLog, Reservation
from ..services import actions, notify, slack_ui

logger = logging.getLogger("carfinder.slack")
router = APIRouter(prefix="/slack", tags=["slack"])

SLACK_API = "https://slack.com/api"


# ------------------------------------------------------------------ security

def verify(headers, body: bytes, secret: str | None = None, now: float | None = None) -> bool:
    """Slack request signing (v0)."""
    secret = secret if secret is not None else os.environ.get("SLACK_SIGNING_SECRET", "")
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not secret or not ts or not sig:
        return False
    try:
        if abs((now or time.time()) - int(ts)) > 300:       # replay protection
            return False
    except ValueError:
        return False
    base = f"v0:{ts}:".encode() + body
    mine = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, sig)


def admins() -> dict[str, str]:
    """Slack user id → admin login email."""
    out = {}
    for part in (os.environ.get("SLACK_ADMINS") or "").split(","):
        if ":" in part:
            uid, email = part.split(":", 1)
            if uid.strip() and email.strip():
                out[uid.strip()] = email.strip().lower()
    return out


async def agent_for(session, slack_user: str) -> Agent | None:
    email = admins().get(slack_user)
    if not email:
        return None
    a = (await session.execute(select(Agent).where(Agent.email == email))).scalar_one_or_none()
    return a if a and a.active else None


# ------------------------------------------------------------------ slack io

def _eph(text: str, blocks: list | None = None, replace: bool = False) -> dict:
    body = {"response_type": "ephemeral", "text": text}
    if blocks:
        body["blocks"] = blocks
    if replace:
        body["replace_original"] = True
    return body


async def respond(response_url: str, body: dict) -> None:
    if not response_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(response_url, json=body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack respond failed: %s", type(exc).__name__)


async def api(method: str, payload: dict) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{SLACK_API}/{method}", json=payload,
                         headers={"Authorization": f"Bearer {token}"})
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "error": f"http {r.status_code}"}


async def download(url: str) -> bytes:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200 or r.headers.get("content-type", "").startswith("text/html"):
        raise RuntimeError("could not download the file from Slack (is files:read granted?)")
    return r.content


# ------------------------------------------------------------------ lookups

_OPEN = ("cancelled", "fulfilled")


async def find(session, q: str) -> list[Reservation]:
    q = q.strip()
    if not q:
        return []
    like = f"%{q.lower()}%"
    conds = [func.lower(Reservation.customer_name).like(like),
             func.lower(Reservation.customer_email).like(like),
             Reservation.customer_phone.like(f"%{re.sub(r'[^0-9]', '', q) or q}%"),
             func.lower(Reservation.supplier_reservation_number).like(like)]
    if re.fullmatch(r"[0-9a-fA-F]{4,32}", q):
        conds.append(Reservation.id.like(f"{q.lower()}%"))
    rows = (await session.execute(select(Reservation).where(or_(*conds))
                                  .order_by(Reservation.created_at.desc()).limit(30))).scalars().all()
    return list(rows)


async def by_ref(session, ref: str) -> Reservation | None:
    ref = ref.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{6,32}", ref):
        return None
    rows = (await session.execute(select(Reservation).where(
        Reservation.id.like(f"{ref}%")).limit(2))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def open_rows(session, newest_first: bool = False) -> list[Reservation]:
    order = Reservation.created_at.desc() if newest_first else Reservation.created_at
    return list((await session.execute(select(Reservation).where(
        Reservation.status.notin_(_OPEN)).order_by(order))).scalars().all())


async def untouched(session) -> list[Reservation]:
    rows = [r for r in await open_rows(session) if not notify._is_test(r)]
    if not rows:
        return []
    touched = set((await session.execute(select(AuditLog.reservation_id).where(
        AuditLog.reservation_id.in_([r.id for r in rows]),
        AuditLog.agent_email.notin_(list(notify.SYSTEM_ACTORS))))).scalars().all())
    return [r for r in rows if r.id not in touched]


# ------------------------------------------------------------------ /rentals

def _login_url() -> str:
    base = notify.admin_base()
    return f"{base}/admin/login" if base else "/admin/login"


async def run_command(user: str, text: str, response_url: str) -> None:
    parts = text.strip().split()
    sub = parts[0].lower() if parts else "help"
    args = parts[1:]
    try:
        async with session_factory()() as s:
            agent = await agent_for(s, user)
            if agent is None:
                await respond(response_url, _eph(
                    ":lock: You're not allowed to use this. The owner must add your Slack ID "
                    f"`{user}` to SLACK_ADMINS on Railway."))
                return
            await _dispatch(s, agent, sub, args, response_url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("slack command failed")
        await respond(response_url, _eph(f":x: Something went wrong: {type(exc).__name__}"))


async def _dispatch(s, agent: Agent, sub: str, args: list[str], response_url: str) -> None:
    say = lambda text, blocks=None: respond(response_url, _eph(text, blocks))  # noqa: E731

    if sub == "today":
        await say(await notify.digest_text(s))
    elif sub == "untouched":
        rows = await untouched(s)
        await say(f"{len(rows)} untouched", slack_ui.list_blocks("Untouched bookings", rows))
    elif sub == "open":
        rows = await open_rows(s, newest_first=True)
        await say(f"{len(rows)} open", slack_ui.list_blocks("Open bookings", rows))
    elif sub == "find":
        rows = await find(s, " ".join(args))
        await say(f"{len(rows)} found", slack_ui.list_blocks(f"Results for “{' '.join(args)}”", rows))
    elif sub == "capture-due":
        from .. import cli
        captured, failed = await cli._capture_due()
        await say(f":moneybag: Auto-capture done: {len(captured)} captured, {len(failed)} failed.")
    elif sub == "users":
        rows = (await s.execute(select(Agent).order_by(Agent.email))).scalars().all()
        lines = [f"• {a.email} — {a.name or '—'} · {a.role}" + ("" if a.active else " · *disabled*")
                 for a in rows]
        await say("*Admin logins*\n" + ("\n".join(lines) or "none"))
    elif sub in ("add-user", "adduser"):
        await _add_user(s, agent, args, say)
    elif sub in ("reset-password", "disable-user", "enable-user"):
        await _user_change(s, agent, sub, args, say)
    else:
        r = await by_ref(s, sub)
        if r:
            await say(f"Booking {r.id[:8].upper()}", slack_ui.card(r))
        else:
            await say(slack_ui.HELP)


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _add_user(s, agent: Agent, args: list[str], say) -> None:
    if agent.role != "admin":
        await say(":lock: Only admins can add users.")
        return
    if not args or not _EMAIL.match(args[0]):
        await say("Usage: `/rentals add-user <email> <name> [admin]`")
        return
    email = args[0].lower()
    role = "admin" if args[-1].lower() == "admin" and len(args) > 1 else "agent"
    name = " ".join(args[1:-1] if role == "admin" else args[1:]).strip()
    if (await s.execute(select(Agent).where(Agent.email == email))).scalar_one_or_none():
        await say(f"{email} already has a login. Use `reset-password` or `enable-user`.")
        return
    pw = secrets.token_urlsafe(9)
    s.add(Agent(email=email, name=name, role=role, password_hash=hash_password(pw)))
    await s.commit()
    logger.info("slack: %s added user %s (%s)", agent.email, email, role)
    await say(f":bust_in_silhouette: Created *{email}* ({role}).\nLogin: {_login_url()}\n"
              f"Temporary password: `{pw}`\n_Only you can see this. Send it to them privately._")


async def _user_change(s, agent: Agent, sub: str, args: list[str], say) -> None:
    if agent.role != "admin":
        await say(":lock: Only admins can change users.")
        return
    if not args:
        await say(f"Usage: `/rentals {sub} <email>`")
        return
    a = (await s.execute(select(Agent).where(Agent.email == args[0].lower()))).scalar_one_or_none()
    if not a:
        await say(f"No login for {args[0]}.")
        return
    if sub == "reset-password":
        pw = secrets.token_urlsafe(9)
        a.password_hash = hash_password(pw)
        await s.commit()
        await say(f"New temporary password for *{a.email}*: `{pw}`\n_Only you can see this._")
    elif sub == "disable-user":
        if a.id == agent.id:
            await say("You can't disable yourself.")
            return
        a.active = False
        await s.commit()
        await say(f"*{a.email}* disabled.")
    else:
        a.active = True
        await s.commit()
        await say(f"*{a.email}* enabled.")


@router.post("/command")
async def command(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not verify(request.headers, body):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    user, text = form.get("user_id", ""), form.get("text", "")
    sub = (text.strip().split() or ["help"])[0].lower()
    if sub == "whoami":
        known = user in admins()
        return _eph(f"Your Slack member ID is `{user}`."
                    + ("" if known else "\nTo give yourself access, set this on the Railway "
                       f"backend: `SLACK_ADMINS={user}:<your admin login email>`"))
    if sub == "help" and user not in admins():
        return _eph(slack_ui.HELP)
    # Slack wants an answer within 3s: reply empty now, post the result to response_url.
    background.add_task(run_command, user, text, form.get("response_url", ""))
    return Response(status_code=200)


# ------------------------------------------------------------------ buttons + modals

async def run_action(user: str, rid: str, action: str, value: str, response_url: str,
                     replace: bool = True) -> None:
    try:
        async with session_factory()() as s:
            agent = await agent_for(s, user)
            if agent is None:
                await respond(response_url, _eph(":lock: You're not allowed to do that."))
                return
            r = await s.get(Reservation, rid)
            if not r:
                await respond(response_url, _eph("Booking not found."))
                return
            res = await actions.perform(s, r, agent, action, value)
            await s.refresh(r)
            who = agent.name or agent.email
            note = (f":white_check_mark: {res.message} — {who}" if res.ok
                    else f":x: {res.error_text or res.message} ({who})")
            await respond(response_url, {**_eph(note, slack_ui.card(r, note)),
                                         "replace_original": replace})
    except Exception as exc:  # noqa: BLE001
        logger.exception("slack action failed")
        await respond(response_url, _eph(f":x: Something went wrong: {type(exc).__name__}"))


async def run_confirm(user: str, rid: str, values: dict, response_url: str) -> None:
    try:
        async with session_factory()() as s:
            agent = await agent_for(s, user)
            r = await s.get(Reservation, rid) if agent else None
            if not agent or not r:
                await respond(response_url, _eph(":lock: Not allowed or booking not found."))
                return
            ref = _val(values, "supplier_ref")
            pol = _val(values, "policy")
            if ref != (r.supplier_reservation_number or ""):
                await actions.perform(s, r, agent, "supplier_ref", ref)
            if pol and pol != (r.policy_number or ""):
                await actions.perform(s, r, agent, "policy", pol)
            files = (((values.get("doc") or {}).get("file") or {}).get("files")) or []
            if files:
                f = files[0]
                url = f.get("url_private_download") or f.get("url_private")
                blob = await download(url)
                up = await actions.attach_document(s, r, agent, blob, f.get("name") or "confirmation.pdf")
                if not up.ok:
                    await respond(response_url, _eph(f":x: {up.error_text}"))
                    return
            await run_action_in(s, r, agent, "confirm_send", response_url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("slack confirm failed")
        await respond(response_url, _eph(f":x: Could not send the confirmation: {exc}"))


async def run_action_in(s, r, agent, action: str, response_url: str, value: str = "") -> None:
    res = await actions.perform(s, r, agent, action, value)
    await s.refresh(r)
    who = agent.name or agent.email
    note = (f":white_check_mark: {res.message} — {who}" if res.ok
            else f":x: {res.error_text or res.message} ({who})")
    await respond(response_url, {**_eph(note, slack_ui.card(r, note)), "replace_original": True})


def _val(values: dict, block: str) -> str:
    return ((((values.get(block) or {}).get("v") or {}).get("value")) or "").strip()


MODALS = {"cancel": slack_ui.cancel_modal, "note": slack_ui.note_modal,
          "confirm": slack_ui.confirm_modal}


@router.post("/interact")
async def interact(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not verify(request.headers, body):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    payload = json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
    user = (payload.get("user") or {}).get("id", "")
    ptype = payload.get("type")

    if ptype == "block_actions":
        act = (payload.get("actions") or [{}])[0]
        action_id = act.get("action_id", "")
        if not action_id.startswith("bk:") or action_id == "bk:open":
            return Response(status_code=200)
        action, rid = action_id[3:], act.get("value", "")
        response_url = payload.get("response_url", "")
        if user not in admins():
            background.add_task(respond, response_url, _eph(":lock: You're not allowed to do that."))
            return Response(status_code=200)
        if action == "show":
            async with session_factory()() as s:
                r = await s.get(Reservation, rid)
                if r:
                    background.add_task(respond, response_url,
                                        _eph(f"Booking {r.id[:8].upper()}", slack_ui.card(r)))
            return Response(status_code=200)
        if action in MODALS:
            # Forms must open within 3s of the click, so do it now.
            async with session_factory()() as s:
                r = await s.get(Reservation, rid)
            if r:
                res = await api("views.open", {"trigger_id": payload.get("trigger_id"),
                                               "view": MODALS[action](r, response_url)})
                if not res.get("ok"):
                    background.add_task(respond, response_url, _eph(
                        f":x: Couldn't open the form ({res.get('error')}). "
                        "Is SLACK_BOT_TOKEN set?"))
            return Response(status_code=200)
        background.add_task(run_action, user, rid, action, "", response_url)
        return Response(status_code=200)

    if ptype == "view_submission":
        view = payload.get("view") or {}
        meta = json.loads(view.get("private_metadata") or "{}")
        rid, response_url = meta.get("rid", ""), meta.get("response_url", "")
        values = (view.get("state") or {}).get("values") or {}
        if user not in admins():
            return {"response_action": "errors",
                    "errors": {next(iter(values), "x"): "You're not allowed to do that."}}
        cb = view.get("callback_id")
        if cb == "cancel_modal":
            background.add_task(run_action, user, rid, "cancel", _val(values, "reason"), response_url)
        elif cb == "note_modal":
            background.add_task(run_action, user, rid, "note", _val(values, "note"), response_url)
        elif cb == "confirm_modal":
            files = (((values.get("doc") or {}).get("file") or {}).get("files")) or []
            async with session_factory()() as s:
                r = await s.get(Reservation, rid)
                has_doc = bool(r and r.confirmation_doc)
            if not files and not has_doc:
                return {"response_action": "errors", "errors": {"doc": "Attach the supplier PDF."}}
            if not _val(values, "supplier_ref"):
                return {"response_action": "errors",
                        "errors": {"supplier_ref": "Enter the supplier reservation number."}}
            background.add_task(run_confirm, user, rid, values, response_url)
        return {"response_action": "clear"}

    return Response(status_code=200)
