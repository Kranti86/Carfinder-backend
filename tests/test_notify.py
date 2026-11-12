"""Slack alerts + daily digest."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, make_session
from app.main import app
from app.services import notify
from tests.test_checkout import (CUSTOMER, _get_res, _make_agent, _update_res, headers, run,
                                 seed_quote)

NOW = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []

    async def fake(text, blocks=None):
        box.append(text)
        return True
    monkeypatch.setattr(notify, "slack", fake)
    return box


def res(**kw):
    base = dict(id="abcdef1234567890", customer_name="Ann Lee", customer_email="ann@mail.co",
                customer_phone="+15550001111", vehicle_name="Toyota Corolla", city="Miami",
                pickup_datetime="2026-12-20T10:00", status="new", payment_status="none",
                created_at=NOW - timedelta(hours=3), authorized_at=None, confirmation_sent=False,
                service_fee=20.0, insurance_price=0.0, amount_authorized=0.0,
                amount_captured=0.0)
    base.update(kw)
    r = NS(**base)
    r.pay_now_total = r.service_fee + r.insurance_price
    return r


def audit(who, action, hours_ago=1):
    return NS(agent_email=who, action=action, created_at=NOW - timedelta(hours=hours_ago))


def test_redacts_webhook_url_from_logs():
    rec = logging.LogRecord("httpx", logging.INFO, "", 0, 'HTTP Request: %s %s "%s"',
                            ("POST", "https://hooks.slack.com/services/T0/B0/secret", "200 OK"),
                            None)
    for f in logging.getLogger("httpx").filters:
        f.filter(rec)
    assert "secret" not in rec.getMessage() and "[redacted]" in rec.getMessage()


def test_digest_sections():
    untouched = res(id="u" * 16, customer_name="Una Touched", created_at=NOW - timedelta(days=2))
    worked = res(id="w" * 16, customer_name="Wes Worked", status="authorized",
                 payment_status="authorized", authorized_at=NOW - timedelta(days=2, hours=12),
                 amount_authorized=20.0)
    declined = res(id="d" * 16, customer_name="Dee Clined", payment_status="failed",
                   customer_phone="+15559998888")
    soon = res(id="s" * 16, customer_name="Sam Soon", status="captured", payment_status="captured",
               pickup_datetime=(NOW + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M"))
    test = res(id="t" * 16, customer_name="TEST Booking", customer_email="x@example.com")
    audits = {"w" * 16: [audit("agent@ops", "note")], "s" * 16: [audit("agent@ops", "note")],
              "d" * 16: [audit("system", "authorize_failed")]}
    text = notify.build_digest([untouched, worked, declined, soon, test], audits, NOW)

    assert "Daily summary" in text and "09:30 IST" in text
    unt = text.split("Untouched")[1].split("\n\n")[0]
    assert "Una Touched" in unt and "waiting 2d" in unt
    assert "Dee Clined" in unt                      # only system touched it
    assert "Wes Worked" not in unt and "Sam Soon" not in unt
    assert "Pickup within 48h" in text and "Sam Soon" in text.split("Pickup within 48h")[1]
    assert "+15559998888" in text.split("Card declined")[1]
    assert "Auto-capture due in next 24h: *1*" in text   # Wes, held 2.5 days
    assert "TEST Booking" not in text


def test_digest_all_clear():
    r = res(status="fulfilled", created_at=NOW - timedelta(days=9))
    text = notify.build_digest([r], {}, NOW)
    assert "Nothing needs attention" in text


def test_new_reservation_alert(client, sent):
    job, vid = seed_quote(client)
    client.post("/requests", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER})
    assert sent and ":new:" in sent[-1] and "Jane Driver" in sent[-1]


def test_admin_and_webhook_alerts_no_duplicates(client, sent, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ALLOW_UNVERIFIED", "true")
    job, vid = seed_quote(client)
    order_id = client.post("/paypal/create-order", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()["order_id"]
    d = client.post("/requests", headers=headers(),
                    json={"paypal_order_id": order_id, "customer": CUSTOMER}).json()
    assert "card hold approved" in sent[-1]

    aid = run(client, _make_agent)
    client.cookies.set(COOKIE_NAME, make_session(aid))
    client.post(f"/admin/reservations/{d['id']}/action", data={"action": "capture"},
                follow_redirects=False)
    client.cookies.clear()
    assert "Payment captured" in sent[-1]
    n = len(sent)

    # PayPal then echoes the capture we made: must NOT alert twice.
    cap = run(client, _get_res, d["id"]).capture_ref
    client.post("/paypal/webhook", json={"event_type": "PAYMENT.CAPTURE.COMPLETED",
                                         "resource": {"id": cap, "custom_id": d["id"],
                                                      "amount": {"value": "42.00"}}})
    assert len(sent) == n

    # A refund done in PayPal directly does alert.
    client.post("/paypal/webhook", json={"event_type": "PAYMENT.CAPTURE.REFUNDED",
                                         "resource": {"id": "R1", "custom_id": d["id"]}})
    assert "Refunded" in sent[-1]


def test_capture_run_summary(client, sent):
    from app import cli
    job, vid = seed_quote(client)
    order_id = client.post("/paypal/create-order", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()["order_id"]
    d = client.post("/requests", headers=headers(),
                    json={"paypal_order_id": order_id, "customer": CUSTOMER}).json()
    run(client, _update_res, d["id"],
        authorized_at=datetime.now(timezone.utc) - timedelta(days=4))
    run(client, cli._capture_due)
    assert any("Auto-capture" in t and "captured" in t for t in sent)


def test_admin_login_rate_limited(client):
    from app.config import get_settings
    s = get_settings()
    old = s.login_rate_limit
    s.login_rate_limit = 3
    try:
        codes = [client.post("/admin/login", data={"email": "x@y.z", "password": "bad"},
                             follow_redirects=False).status_code for _ in range(5)]
    finally:
        s.login_rate_limit = old
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_opted_out_booking_not_reported_to_ads(client):
    import uuid
    from app.db import session_factory
    from app.models_db import Reservation
    from app.services import conversions
    g_ok, g_out = "GCLID-OK-" + uuid.uuid4().hex[:6], "GCLID-OUT-" + uuid.uuid4().hex[:6]

    async def go():
        async with session_factory()() as s:
            for g, opt in ((g_ok, False), (g_out, True)):
                s.add(Reservation(id=uuid.uuid4().hex, gclid=g, ads_opt_out=opt,
                                  status="captured", payment_status="captured", service_fee=20))
            await s.commit()
            return (await conversions.conversions_csv(s))[0]
    text = run(client, go)
    assert g_ok in text and g_out not in text
