"""Slack remote control: signatures, permissions, commands, buttons, modals, users."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import session_factory
from app.main import app
from app.models_db import Agent, AuditLog
from app.routers import slack as sl
from app.services import notify
from tests.test_checkout import CUSTOMER, _get_res, _update_res, headers, run, seed_quote

SECRET = "test-signing-secret"
OWNER = "UOWNER1"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def slack_env(client, monkeypatch):
    email = f"owner-{uuid.uuid4().hex[:6]}@ops.test"

    async def mk():
        async with session_factory()() as s:
            s.add(Agent(email=email, name="Owner", role="admin", password_hash=hash_password("x")))
            await s.commit()
    run(client, mk)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_ADMINS", f"{OWNER}:{email}")
    out: list[dict] = []

    async def fake_respond(url, body):
        out.append(body)

    async def fake_slack(text, blocks=None):
        return True
    monkeypatch.setattr(sl, "respond", fake_respond)
    monkeypatch.setattr(notify, "slack", fake_slack)
    return out


def signed(body: str, secret: str = SECRET, ts: int | None = None) -> dict:
    ts = ts or int(time.time())
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": str(ts), "x-slack-signature": sig,
            "content-type": "application/x-www-form-urlencoded"}


def cmd(client, text, user=OWNER):
    body = urlencode({"user_id": user, "text": text, "response_url": "https://hooks.test/r"})
    return client.post("/slack/command", content=body, headers=signed(body))


def click(client, action, rid, user=OWNER, trigger="T1"):
    payload = {"type": "block_actions", "user": {"id": user}, "trigger_id": trigger,
               "response_url": "https://hooks.test/r",
               "actions": [{"action_id": f"bk:{action}", "value": rid}]}
    body = urlencode({"payload": json.dumps(payload)})
    return client.post("/slack/interact", content=body, headers=signed(body))


def submit(client, callback, rid, values, user=OWNER):
    payload = {"type": "view_submission", "user": {"id": user},
               "view": {"callback_id": callback, "state": {"values": values},
                        "private_metadata": json.dumps({"rid": rid,
                                                        "response_url": "https://hooks.test/r"})}}
    body = urlencode({"payload": json.dumps(payload)})
    return client.post("/slack/interact", content=body, headers=signed(body))


def held_booking(client) -> str:
    job, vid = seed_quote(client)
    order_id = client.post("/paypal/create-order", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()["order_id"]
    d = client.post("/requests", headers=headers(),
                    json={"paypal_order_id": order_id, "customer": CUSTOMER}).json()
    assert d["payment_status"] == "authorized"
    return d["id"]


# ------------------------------------------------------------------ security

def test_signature_checks():
    body = b"a=1"
    ts = int(time.time())
    good = signed("a=1", ts=ts)
    assert sl.verify(good, body, SECRET)
    assert not sl.verify(good, b"a=2", SECRET)                     # tampered
    assert not sl.verify(signed("a=1", "other", ts), body, SECRET)  # wrong secret
    old = signed("a=1", ts=ts - 600)
    assert not sl.verify(old, body, SECRET)                        # replay


def test_unsigned_rejected(client, slack_env):
    r = client.post("/slack/command", content="user_id=U1&text=today",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 401


def test_whoami_and_strangers_blocked(client, slack_env):
    r = cmd(client, "whoami", user="USTRANGER")
    assert "USTRANGER" in r.json()["text"] and "SLACK_ADMINS" in r.json()["text"]
    cmd(client, "today", user="USTRANGER")
    assert "not allowed" in slack_env[-1]["text"]
    rid = held_booking(client)
    click(client, "capture", rid, user="USTRANGER")
    assert "not allowed" in slack_env[-1]["text"]
    assert run(client, _get_res, rid).payment_status == "authorized"


# ------------------------------------------------------------------ commands

def test_commands(client, slack_env):
    rid = held_booking(client)
    cmd(client, "today")
    assert "Daily summary" in slack_env[-1]["text"]
    cmd(client, "untouched")
    assert "Untouched" in json.dumps(slack_env[-1]["blocks"])
    assert rid not in json.dumps(slack_env[-1]["blocks"])     # example.com = test booking
    cmd(client, "open")
    assert any(rid in json.dumps(b) for b in slack_env[-1]["blocks"])
    cmd(client, rid[:8].upper())
    card = json.dumps(slack_env[-1]["blocks"])
    assert "bk:capture" in card and "Jane Driver" in card
    cmd(client, "find jane@example")
    assert "Results" in json.dumps(slack_env[-1]["blocks"])
    cmd(client, "gibberish")
    assert "/rentals" in slack_env[-1]["text"]


def test_user_management(client, slack_env):
    email = f"new-{uuid.uuid4().hex[:6]}@ops.test"
    cmd(client, f"add-user {email} Priya Sharma")
    assert "Temporary password" in slack_env[-1]["text"]
    cmd(client, "users")
    assert email in slack_env[-1]["text"]
    cmd(client, f"disable-user {email}")
    assert "disabled" in slack_env[-1]["text"]
    cmd(client, f"reset-password {email}")
    assert "New temporary password" in slack_env[-1]["text"]


# ------------------------------------------------------------------ buttons + modals

def test_capture_button_then_refund(client, slack_env):
    rid = held_booking(client)
    click(client, "claim", rid)
    assert "Claimed" in slack_env[-1]["text"]
    click(client, "capture", rid)
    assert "Captured" in slack_env[-1]["text"] and slack_env[-1]["replace_original"]
    res = run(client, _get_res, rid)
    assert res.payment_status == "captured"
    click(client, "refund", rid)
    assert "Refunded" in slack_env[-1]["text"]
    assert run(client, _get_res, rid).payment_status == "refunded"

    async def who():
        async with session_factory()() as s:
            from sqlalchemy import select
            return {a.action: a.agent_email for a in (await s.execute(
                select(AuditLog).where(AuditLog.reservation_id == rid))).scalars()}
    log = run(client, who)
    assert log["capture"].startswith("owner-") and log["refund"].startswith("owner-")


def test_cancel_modal_releases_hold(client, slack_env, monkeypatch):
    opened = []

    async def fake_api(method, payload):
        opened.append((method, payload["view"]["callback_id"]))
        return {"ok": True}
    monkeypatch.setattr(sl, "api", fake_api)
    rid = held_booking(client)
    click(client, "cancel", rid)
    assert opened[-1] == ("views.open", "cancel_modal")
    r = submit(client, "cancel_modal", rid, {"reason": {"v": {"value": "customer asked"}}})
    assert r.json() == {"response_action": "clear"}
    res = run(client, _get_res, rid)
    assert res.status == "cancelled" and res.payment_status == "voided"


def test_confirm_modal_with_pdf(client, slack_env, monkeypatch):
    async def fake_download(url):
        assert url == "https://files.slack.test/doc.pdf"
        return b"%PDF-1.4 test"
    monkeypatch.setattr(sl, "download", fake_download)
    rid = held_booking(client)
    # Missing PDF → inline error, nothing sent
    r = submit(client, "confirm_modal", rid, {"supplier_ref": {"v": {"value": "HZ-123"}},
                                              "doc": {"file": {"files": []}}})
    assert r.json()["response_action"] == "errors" and "doc" in r.json()["errors"]
    r = submit(client, "confirm_modal", rid, {
        "supplier_ref": {"v": {"value": "HZ-123"}}, "policy": {"v": {"value": ""}},
        "doc": {"file": {"files": [{"name": "hertz.pdf",
                                    "url_private_download": "https://files.slack.test/doc.pdf"}]}}})
    assert r.json() == {"response_action": "clear"}
    res = run(client, _get_res, rid)
    assert res.confirmation_sent and res.supplier_reservation_number == "HZ-123"
    assert bytes(res.confirmation_doc).startswith(b"%PDF") and res.status == "confirmed"
    assert "Final confirmation emailed" in slack_env[-1]["text"]
    click(client, "fulfill", rid)
    assert run(client, _get_res, rid).status == "fulfilled"


def test_alert_has_buttons_only_when_interactive(client, monkeypatch):
    from app.services import slack_ui
    rid = held_booking(client)
    r = run(client, _get_res, rid)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    assert slack_ui.actions_block(r) == []
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    ids = [e["action_id"] for e in slack_ui.actions_block(r)[0]["elements"]]
    assert "bk:capture" in ids and "bk:refund" not in ids and "bk:cancel" in ids


def test_admin_upload_no_longer_crashes(client):
    from app.auth import COOKIE_NAME, make_session
    from tests.test_checkout import _make_agent
    rid = held_booking(client)
    aid = run(client, _make_agent)
    client.cookies.set(COOKIE_NAME, make_session(aid))
    r = client.post(f"/admin/reservations/{rid}/upload",
                    files={"file": ("conf.pdf", b"%PDF-1.4", "application/pdf")},
                    follow_redirects=False)
    client.cookies.clear()
    assert r.status_code == 303
    assert run(client, _get_res, rid).confirmation_doc_name == "conf.pdf"
