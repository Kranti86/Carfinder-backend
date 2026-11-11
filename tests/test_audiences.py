"""Customer Match audience exports."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, make_session
from app.config import get_settings
from app.db import session_factory
from app.main import app
from app.models_db import Reservation
from app.services import audiences

from tests.test_checkout import _make_agent, run

H = lambda v: hashlib.sha256(v.encode()).hexdigest()


def test_normalize_email_and_phone():
    assert audiences.normalize_email("  Jane.Doe+promo@GMail.com ") == "janedoe@gmail.com"
    assert audiences.normalize_email("Bob.Smith@Company.co") == "bob.smith@company.co"
    assert audiences.normalize_email("not-an-email") == ""
    assert audiences.normalize_phone("(555) 123-4567") == "+15551234567"
    assert audiences.normalize_phone("1-555-123-4567") == "+15551234567"
    assert audiences.normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert audiences.normalize_phone("12345") == ""


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


async def _seed(rows):
    async with session_factory()() as s:
        for kw in rows:
            s.add(Reservation(id=uuid.uuid4().hex, **kw))
        await s.commit()


def test_segments_and_exclusions(client):
    tag = uuid.uuid4().hex[:6]
    em = lambda n: f"{n}.{tag}@mail.co"
    run(client, _seed, [
        # confirmed customer, booked twice (repeat), once with insurance
        dict(customer_name="Ann Lee", customer_email=em("ann"), customer_phone="5550001111",
             status="captured", payment_status="captured", insurance_price=30.0),
        dict(customer_name="Ann Lee", customer_email=em("ann").upper(), customer_phone="",
             status="fulfilled", payment_status="captured"),
        # reserved, never paid
        dict(customer_name="Ben Ray", customer_email=em("ben"), customer_phone="5550002222",
             status="new", payment_status="none"),
        # cancelled
        dict(customer_name="Cat Ng", customer_email=em("cat"), customer_phone="",
             status="cancelled", payment_status="voided"),
        # rejected cookies → never exported
        dict(customer_name="Dan Opt", customer_email=em("dan"), customer_phone="5550003333",
             status="captured", payment_status="captured", ads_opt_out=True),
        # test booking → never exported
        dict(customer_name="TEST Booking", customer_email=f"t.{tag}@example.com",
             status="captured", payment_status="captured"),
    ])

    async def seg(name):
        async with session_factory()() as s:
            return {e for e, _ in await audiences.collect(s, name) if tag in e}
    ann, ben, cat = em("ann"), em("ben"), em("cat")
    assert run(client, seg, "customers") == {ann}
    assert run(client, seg, "repeat-customers") == {ann}
    assert run(client, seg, "insurance-buyers") == {ann}
    assert run(client, seg, "all-bookers") == {ann, ben, cat}
    assert run(client, seg, "cancelled") == {cat}

    async def csv_text():
        async with session_factory()() as s:
            return (await audiences.audience_csv(s, "customers"))[0]
    text = run(client, csv_text)
    assert text.splitlines()[0] == "Email,Phone"
    assert f"{H(ann)},{H('+15550001111')}" in text          # hashed, never raw
    assert ann not in text and "5550001111" not in text
    assert H(em("dan")) not in text                          # opted out


def test_audience_endpoints_protected(client):
    s = get_settings()
    old = s.conversions_export_token
    s.conversions_export_token = "tok123"
    try:
        assert client.get("/exports/audience/customers.csv?key=bad").status_code == 403
        assert client.get("/exports/audience/nope.csv?key=tok123").status_code == 404
        r = client.get("/exports/audience/customers.csv?key=tok123")
        assert r.status_code == 200 and r.text.startswith("Email,Phone")
    finally:
        s.conversions_export_token = old
    # admin pages need a login
    assert "/admin/login" in client.get("/admin/exports", follow_redirects=False).headers["location"]
    aid = run(client, _make_agent)
    client.cookies.set(COOKIE_NAME, make_session(aid))
    page = client.get("/admin/exports")
    dl = client.get("/admin/audience/all-bookers.csv")
    client.cookies.clear()
    assert page.status_code == 200 and "repeat-customers" in page.text
    assert dl.status_code == 200 and dl.text.startswith("Email,Phone")


def test_reject_cookie_flag_saved(client):
    from tests.test_checkout import CUSTOMER, headers, seed_quote, _get_res
    job, vid = seed_quote(client)
    d = client.post("/requests", headers=headers(), json={
        "job_id": job, "vehicle_id": vid, "customer": CUSTOMER,
        "source": {"consent": "denied"}}).json()
    assert run(client, _get_res, d["id"]).ads_opt_out is True
