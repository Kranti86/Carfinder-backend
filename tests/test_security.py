"""Security hardening from the Nov 2026 review."""

from __future__ import annotations

import uuid

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, hash_password, make_session
from app.config import get_settings
from app.db import session_factory
from app.main import app
from app.models_db import Agent
from app.services import notify, security
from tests.test_checkout import CUSTOMER, ORIGIN, headers, run, seed_quote


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def slack_box(monkeypatch):
    box: list[str] = []

    async def fake(text, blocks=None):
        box.append(text)
        return True
    monkeypatch.setattr(notify, "slack", fake)
    return box


def _agent(client, pw="Correct-Horse-9", role="admin"):
    email = f"sec-{uuid.uuid4().hex[:8]}@ops.test"

    async def mk():
        async with session_factory()() as s:
            a = Agent(email=email, name="Sec", role=role, password_hash=hash_password(pw))
            s.add(a)
            await s.commit()
            return a.id
    return email, pw, run(client, mk)


def _ip():
    return {"x-forwarded-for": f"198.51.100.{uuid.uuid4().int % 250}"}


# ------------------------------------------------------------------ canary

def test_canary_hidden_without_token(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "canary_token", "")
    assert client.get("/canary").status_code == 404
    monkeypatch.setattr(s, "canary_token", "t0ken")
    assert client.get("/canary?token=wrong").status_code == 403


# ------------------------------------------------------------------ rate-limit bypass

def test_session_header_cannot_reset_rate_limit(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "lookup_rate_limit", 3)
    ip = _ip()
    codes = [client.post("/lookup", headers={**ORIGIN, **ip, "x-session-id": uuid.uuid4().hex},
                         json={"ref": "deadbeef", "email": "x@y.z"}).status_code for _ in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]


# ------------------------------------------------------------------ email abuse

def test_unpaid_request_sends_no_email_paid_does(client, monkeypatch):
    from app.services import email_service
    sent = []
    monkeypatch.setattr(email_service, "send_confirmation", lambda d: sent.append(d["customer_email"]))
    job, vid = seed_quote(client)
    victim = {**CUSTOMER, "email": f"victim-{uuid.uuid4().hex[:5]}@mail.co"}
    r = client.post("/requests", headers=headers(), json={"job_id": job, "vehicle_id": vid,
                                                          "customer": victim})
    assert r.status_code == 200 and victim["email"] not in sent       # no payment → no email
    job, vid = seed_quote(client)
    order = client.post("/paypal/create-order", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()["order_id"]
    client.post("/requests", headers=headers(), json={"paypal_order_id": order, "customer": CUSTOMER})
    assert CUSTOMER["email"] in sent                                   # held card → email


# ------------------------------------------------------------------ two-factor

def test_two_factor_enroll_login_replay_and_reset(client, slack_box, monkeypatch):
    email, pw, aid = _agent(client)
    client.cookies.set(COOKIE_NAME, make_session(aid))
    page = client.get("/admin/security").text
    assert "<svg" in page and "Turn on" in page
    secret = page.split('name="secret" value="')[1].split('"')[0]
    totp = pyotp.TOTP(secret)
    assert "bad_code" in client.post("/admin/security", data={"action": "enable", "secret": secret,
                                                              "code": "000000"},
                                     follow_redirects=False).headers["location"]
    client.post("/admin/security", data={"action": "enable", "secret": secret, "code": totp.now()})
    assert any("Two-factor turned ON" in t for t in slack_box)
    client.cookies.clear()

    ip = _ip()
    r = client.post("/admin/login", headers=ip, data={"email": email, "password": pw},
                    follow_redirects=False)
    assert r.status_code == 401 and "6-digit code" in r.text                 # password alone fails
    code = totp.now()
    r = client.post("/admin/login", headers=ip, data={"email": email, "password": pw, "code": code},
                    follow_redirects=False)
    assert r.status_code == 303                                             # password + code works
    assert any("Admin sign-in" in t and email in t for t in slack_box)
    r = client.post("/admin/login", headers=ip, data={"email": email, "password": pw, "code": code},
                    follow_redirects=False)
    assert r.status_code == 401                                             # same code can't be replayed
    client.cookies.clear()

    # super admin can reset it from Slack (lost phone)
    from tests.test_slack import OWNER, cmd, SECRET
    owner_email, _, _ = _agent(client)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_ADMINS", f"{OWNER}:{owner_email}")
    replies = []

    async def fake_respond(url, body):
        replies.append(body)
    from app.routers import slack as sl
    monkeypatch.setattr(sl, "respond", fake_respond)
    cmd(client, f"reset-2fa {email}")
    assert "super admin" in replies[-1]["text"]                              # not super → refused
    monkeypatch.setenv("SUPER_ADMINS", owner_email)
    cmd(client, f"reset-2fa {email}")
    assert "Two-factor reset" in replies[-1]["text"]
    r = client.post("/admin/login", headers=_ip(), data={"email": email, "password": pw},
                    follow_redirects=False)
    assert r.status_code == 303


def test_lockout_alert(client, slack_box, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "login_rate_limit", 2)
    ip = _ip()
    for _ in range(4):
        client.post("/admin/login", headers=ip, data={"email": "x@y.z", "password": "nope"})
    locks = [t for t in slack_box if "Admin login blocked" in t]
    assert len(locks) == 1 and ip["x-forwarded-for"] in locks[0]           # alerted once, not spammed


# ------------------------------------------------------------------ alerts

def test_budget_and_decline_alerts(client, slack_box, monkeypatch):
    from app import budget
    s = get_settings()
    monkeypatch.setattr(s, "upstream_daily_cap", 5)
    from app.cache import get_cache
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y%m%d")

    async def go():
        await get_cache().set_raw(f"upstream_budget:{day}", "0", ttl=60)
        for _ in range(4):
            await budget.spend_credit()
    run(client, go)
    assert any("Search budget 80% used" in t for t in slack_box)

    async def declines():
        for _ in range(3):
            await security.count_and_alert(f"declines-test-{uuid.uuid4().hex}", 3600, 3, "x {n}")
        name = f"declines-{uuid.uuid4().hex}"
        for _ in range(3):
            await security.count_and_alert(name, 3600, 3, ":rotating_light: *{n} card declines")
    run(client, declines)
    assert any("3 card declines" in t for t in slack_box)


# ------------------------------------------------------------------ bot check

def test_turnstile_gate_and_pass(client, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site")

    async def fake_ok(token, ip=""):
        return token == "good"
    monkeypatch.setattr(security, "turnstile_ok", fake_ok)
    body = {"lat": 25.79, "lon": -80.28, "pickup_datetime": "2026-12-20T10:00",
            "dropoff_datetime": "2026-12-23T10:00"}
    ip = _ip()
    assert client.post("/search", headers={**ORIGIN, **ip}, json=body).status_code == 403
    r = client.post("/search", headers={**ORIGIN, **ip, "x-turnstile-token": "good"}, json=body)
    assert r.status_code == 202 and r.headers.get("x-human-pass")
    p = r.headers["x-human-pass"]
    assert client.post("/search", headers={**ORIGIN, **ip, "x-human-pass": p}, json=body).status_code == 202
    other = _ip()                                       # a pass is bound to the IP that earned it
    assert client.post("/search", headers={**ORIGIN, **other, "x-human-pass": p}, json=body).status_code == 403
    assert client.get("/online-config").json()["turnstile_site_key"] == "site"
    # unpaid booking needs the check too
    job, vid = seed_quote(client)
    r = client.post("/requests", headers=headers(), json={"job_id": job, "vehicle_id": vid,
                                                          "customer": CUSTOMER})
    assert r.status_code == 403 and r.json()["error"] == "human_check"


def test_security_headers(client):
    r = client.get("/admin/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "max-age" in r.headers["strict-transport-security"]
    assert "max-age" in client.get("/health").headers["strict-transport-security"]
