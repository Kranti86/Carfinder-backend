"""End-to-end checks for server-side pricing, lookup, webhooks and the portal fixes."""

from __future__ import annotations

import asyncio
import functools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.auth import COOKIE_NAME, hash_password, make_session
from app.budget import BudgetExhausted, spend_credit
from app.config import get_settings
from app.db import session_factory
from app.main import app
from app.models import CarCard, SearchQuery
from app.models_db import Agent, Reservation
from app.services import payments, quotes

ORIGIN = {"origin": "https://online.thecarrentals.globetradee.pro"}
CUSTOMER = {"name": "Jane Driver", "email": "jane@example.com", "phone": "5551234567"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def run(client, fn, *args, **kwargs):
    """Run a coroutine on the app's own event loop (shares the DB engine safely)."""
    return client.portal.call(functools.partial(fn, *args, **kwargs))


def seed_quote(client, base=100.0, fee=42.0) -> tuple[str, str]:
    job_id, vid = uuid.uuid4().hex, "v1"
    q = SearchQuery(lat=25.77, lon=-80.19, pickup_datetime="2026-12-01T10:00",
                    dropoff_datetime="2026-12-04T10:00", label="Miami")
    card = CarCard(vehicle_id=vid, name="Toyota Corolla", supplier="Avis",
                   vehicle_type="Compact", pickup_location="Miami Intl",
                   base_price=base, service_fee=fee, price=base + fee)
    run(client, quotes.store_job_quote, job_id, q, [card])
    return job_id, vid


def headers(**extra):
    # unique session id → its own rate-limit bucket
    return {**ORIGIN, "x-session-id": uuid.uuid4().hex, **extra}


async def _get_res(rid):
    async with session_factory()() as s:
        return await s.get(Reservation, rid)


async def _update_res(rid, **fields):
    async with session_factory()() as s:
        r = await s.get(Reservation, rid)
        for k, v in fields.items():
            setattr(r, k, v)
        await s.commit()


# --------------------------------------------------------------------------- #
# pricing is decided by the server
# --------------------------------------------------------------------------- #
def test_quote_is_server_computed(client):
    job, vid = seed_quote(client)
    r = client.post("/quote", json={"job_id": job, "vehicle_id": vid})
    assert r.status_code == 200
    d = r.json()
    assert d["service_fee"] == 42.0 and d["base_price"] == 100.0 and d["days"] == 3
    # 8.99 × 3 days = 26.97 net, × 1.40 = 37.76
    assert d["insurance"]["price"] == 37.76
    assert d["pay_now_with_insurance"] == 79.76
    assert "net" not in d["insurance"]


def test_requests_ignores_client_prices(client):
    job, vid = seed_quote(client)
    r = client.post("/requests", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "insurance": True, "customer": CUSTOMER,
        "pricing": {"service_fee": 0.01, "base_price": 1, "insurance": {"price": 0.01}}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["service_fee"] == 42.0 and d["insurance_price"] == 37.76 and d["pay_now"] == 79.76
    assert d["base_price"] == 100.0 and d["vehicle_name"] == "Toyota Corolla"
    # margin = fee + insurance commission (37.76 - 26.97)
    assert d["conversion_value"] == round(42.0 + 37.76 - 26.97, 2)


def test_requests_without_quote_rejected(client):
    r = client.post("/requests", headers=headers(),
                    json={"job_id": "nope", "vehicle_id": "v1", "customer": CUSTOMER})
    assert r.status_code == 409 and r.json()["error"] == "quote_expired"


def test_requests_needs_our_origin(client):
    job, vid = seed_quote(client)
    r = client.post("/requests", headers={"x-session-id": uuid.uuid4().hex},
                    json={"job_id": job, "vehicle_id": vid, "customer": CUSTOMER})
    assert r.status_code == 403


def test_requests_validates_customer(client):
    job, vid = seed_quote(client)
    r = client.post("/requests", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": {**CUSTOMER, "email": "nope"}})
    assert r.status_code == 400


def test_requests_rate_limited(client):
    s = get_settings()
    old = s.requests_rate_limit
    s.requests_rate_limit = 2
    try:
        h = headers()
        codes = [client.post("/requests", headers=h, json={}).status_code for _ in range(3)]
        assert codes[-1] == 429
    finally:
        s.requests_rate_limit = old


def test_paid_flow_server_amount_and_idempotent(client):
    job, vid = seed_quote(client)
    o = client.post("/paypal/create-order", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "insurance": True, "amount": 1.00,
        "customer": CUSTOMER})
    assert o.status_code == 200, o.text
    assert o.json()["amount"] == 79.76           # client "amount" ignored
    order_id = o.json()["order_id"]
    body = {"paypal_order_id": order_id, "customer": CUSTOMER,
            # tampering with the car/insurance after approval is ignored too
            "job_id": job, "vehicle_id": "other", "insurance": False}
    r1 = client.post("/requests", headers=headers(), json=body)
    assert r1.status_code == 200, r1.text
    d = r1.json()
    assert d["payment_status"] == "authorized" and d["pay_now"] == 79.76
    res = run(client, _get_res, d["id"])
    assert res.amount_authorized == 79.76 and res.paypal_order_id == order_id
    # retry after a lost response → same reservation, no duplicate
    r2 = client.post("/requests", headers=headers(), json=body)
    assert r2.status_code == 200 and r2.json()["id"] == d["id"] and r2.json()["duplicate"]


def test_unknown_order_rejected(client):
    r = client.post("/requests", headers=headers(),
                    json={"paypal_order_id": "FAKE-123", "customer": CUSTOMER})
    assert r.status_code == 409 and r.json()["error"] == "payment_expired"


def test_held_amount_mismatch_is_voided(client, monkeypatch):
    job, vid = seed_quote(client)
    order_id = client.post("/paypal/create-order", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()["order_id"]
    voided = []

    async def fake_auth(amount, oid, *, reservation_id):
        return payments.PayResult(ok=True, ref="AUTH-X", live=True, amount=1.00)

    async def fake_void(ref):
        voided.append(ref)
        return payments.PayResult(ok=True, ref=ref)

    monkeypatch.setattr(payments, "authorize", fake_auth)
    monkeypatch.setattr(payments, "void", fake_void)
    d = client.post("/requests", headers=headers(),
                    json={"paypal_order_id": order_id, "customer": CUSTOMER}).json()
    assert d["payment_status"] == "failed" and voided == ["AUTH-X"]


# --------------------------------------------------------------------------- #
# lookup can't be used to enumerate
# --------------------------------------------------------------------------- #
def test_lookup_exact_only(client):
    job, vid = seed_quote(client)
    d = client.post("/requests", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()
    ref = d["ref"]
    ok = client.post("/lookup", json={"ref": ref, "email": "JANE@example.com"})
    assert ok.status_code == 200 and ok.json()["id"] == d["id"]
    assert client.post("/lookup", json={"ref": ref, "last_name": "driver"}).status_code == 200
    # wildcards / partial values no longer match
    assert client.post("/lookup", json={"ref": "%", "last_name": "e"}).status_code == 400
    assert client.post("/lookup", json={"ref": ref[:4], "email": CUSTOMER["email"]}).status_code == 400
    assert client.post("/lookup", json={"ref": ref, "last_name": "r"}).status_code == 404
    assert client.post("/lookup", json={"ref": ref, "email": "x@example.com"}).status_code == 404


# --------------------------------------------------------------------------- #
# webhooks
# --------------------------------------------------------------------------- #
def test_webhook_rejected_when_unverifiable(client):
    r = client.post("/paypal/webhook", json={"event_type": "PAYMENT.CAPTURE.COMPLETED",
                                             "resource": {"custom_id": "x"}})
    assert r.status_code == 400


def test_webhook_refund_matches_via_capture_link(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ALLOW_UNVERIFIED", "true")
    job, vid = seed_quote(client)
    d = client.post("/requests", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()
    run(client, _update_res, d["id"], payment_status="captured", capture_ref="CAP-999",
        amount_captured=42.0, status="captured")
    event = {"event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {
        "id": "REFUND-1",
        "links": [{"rel": "up", "href": "https://api-m.paypal.com/v2/payments/captures/CAP-999"}]}}
    r = client.post("/paypal/webhook", json=event)
    assert r.status_code == 200 and r.json()["matched"] is True
    assert run(client, _get_res, d["id"]).payment_status == "refunded"


# --------------------------------------------------------------------------- #
# portal: cancel releases the hold; auto-capture skips cancelled
# --------------------------------------------------------------------------- #
async def _make_agent():
    async with session_factory()() as s:
        a = Agent(email=f"{uuid.uuid4().hex[:6]}@ops.test", password_hash=hash_password("pw"),
                  role="admin")
        s.add(a)
        await s.commit()
        return a.id


def test_cancel_voids_hold_and_capture_skips(client):
    job, vid = seed_quote(client)
    order_id = client.post("/paypal/create-order", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER}).json()["order_id"]
    d = client.post("/requests", headers=headers(),
                    json={"paypal_order_id": order_id, "customer": CUSTOMER}).json()
    assert d["payment_status"] == "authorized"

    aid = run(client, _make_agent)
    client.cookies.set(COOKIE_NAME, make_session(aid))
    r = client.post(f"/admin/reservations/{d['id']}/action", data={"action": "cancel"},
                    follow_redirects=False)
    client.cookies.clear()
    assert r.status_code == 303
    res = run(client, _get_res, d["id"])
    assert res.status == "cancelled" and res.payment_status == "voided"

    # Even a cancelled booking still marked "authorized" must never be auto-captured.
    old = datetime.now(timezone.utc) - timedelta(days=5)
    run(client, _update_res, d["id"], payment_status="authorized", authorized_at=old)
    run(client, cli._capture_due)
    assert run(client, _get_res, d["id"]).payment_status == "authorized"


# --------------------------------------------------------------------------- #
# credit + ops guards
# --------------------------------------------------------------------------- #
def test_detail_endpoints_guarded(client):
    assert client.get("/vehicle/k/v").status_code == 403
    assert client.get("/booking-summary/k/v").status_code == 403


def test_daily_budget_cap():
    s = get_settings()
    old = s.upstream_daily_cap
    s.upstream_daily_cap = 1
    try:
        async def go():
            from app.cache import get_cache
            from datetime import datetime as dt
            await get_cache().set_raw(f"upstream_budget:{dt.now(timezone.utc):%Y%m%d}", "0")
            await spend_credit()
            with pytest.raises(BudgetExhausted):
                await spend_credit()
        asyncio.run(go())
    finally:
        s.upstream_daily_cap = old


def test_metrics_and_docs_hidden(client):
    assert client.get("/metrics").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
