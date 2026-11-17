"""SEO location pages: data capture, publishing rules, pages, sitemap, seeding."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import session_factory
from app.main import app
from app.models import CarCard
from app.models_db import CityStat
from app.services import seo, stats_service
from tests.test_checkout import run

MAIN = {"x-trc-site": "main"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def cards(n=12, base=30.0, days=3):
    types = ["Economy", "Compact", "SUV", "Full-size"]
    out = []
    for i in range(n):
        total = (base + i * 5) * days
        out.append(CarCard(vehicle_id=str(i), name=f"Car {i}", supplier=f"Supplier {i % 4}",
                           vehicle_type=types[i % 4], pickup_location="Airport terminal",
                           price=total))
    return out


def test_per_day_uses_real_trip_length():
    assert seo.trip_days("2026-12-01T10:00", "2026-12-08T10:00") == 7
    assert seo.trip_days("2026-12-01T10:00", "2026-12-01T18:00") == 1
    assert seo.trip_days("2026-12-01T10:00", "2026-12-04T11:00") == 4   # started day counts
    # a 7-day search priced at $30/day must show $30/day, not $70
    st = stats_service.compute(cards(base=30, days=7), 12, days=7)
    assert st["cheapest_per_day"] == 30
    assert st["min_by_type"]["Economy"] == 30


def _place(slug):
    async def go():
        async with session_factory()() as s:
            return await s.get(CityStat, slug)
    return go


def test_real_searches_publish_after_threshold(client):
    lat, lon = 30.0 + uuid.uuid4().int % 1000 / 10000, -90.0   # unique spot, not an airport
    label = f"Testville {uuid.uuid4().hex[:4]}"
    st = stats_service.compute(cards(), 12, days=3)
    slug = run(client, seo.record_search, lat, lon, label, st)
    assert slug == seo.slugify(label)
    row = run(client, _place(slug))
    assert row.kind == "city" and row.searches == 1 and not seo.is_published(row)
    assert client.get(f"/car-rental/{slug}", headers=MAIN).status_code == 404
    # nearby search (1 km away, different label) merges into the same place
    run(client, seo.record_search, lat + 0.009, lon, "Somewhere else", st)
    run(client, seo.record_search, lat, lon, label, st)
    row = run(client, _place(slug))
    assert row.searches == 3 and seo.is_published(row)

    page = client.get(f"/car-rental/{slug}", headers=MAIN)
    assert page.status_code == 200
    html = page.text
    assert f"Car rental at {label}" in html and "$30" in html
    assert '"@type": "FAQPage"' in html and "noindex" not in html
    assert f'rel="canonical" href="https://online.thecarrentals.globetradee.pro/car-rental/{slug}"' in html
    assert "autosearch=1" in html
    # same page straight off the backend domain must not be indexable
    direct = client.get(f"/car-rental/{slug}")
    assert direct.headers.get("x-robots-tag") == "noindex" and 'content="noindex"' in direct.text

    assert slug in client.get("/car-rental", headers=MAIN).text
    assert f"/car-rental/{slug}</loc>" in client.get("/car-rental/sitemap.xml").text
    pop = client.get("/car-rental/popular.json").json()
    assert any(p["url"] == f"/car-rental/{slug}" for p in pop)


def test_airport_searches_map_to_airport_page(client):
    st = stats_service.compute(cards(), 12, days=3)
    slug = run(client, seo.record_search, 25.7960, -80.2869, "miami airport", st)
    assert slug == "miami-airport-mia"
    row = run(client, _place(slug))
    assert row.kind == "airport" and row.iata == "MIA" and row.name == "Miami Airport (MIA)"
    assert seo.is_published(row)          # airports publish on first good data
    assert "Miami Airport (MIA)" in client.get("/car-rental/miami-airport-mia", headers=MAIN).text


def test_thin_or_stale_not_published():
    now = datetime.now(timezone.utc)
    thin = CityStat(slug="x", name="X", kind="airport", searches=5, seeded=1,
                    stats=json.dumps({"car_count": 4, "cheapest_per_day": 20}), updated_at=now)
    old = CityStat(slug="y", name="Y", kind="airport", searches=5, seeded=1,
                   stats=json.dumps({"car_count": 50, "cheapest_per_day": 20}),
                   updated_at=now - timedelta(days=60))
    assert not seo.is_published(thin) and not seo.is_published(old)


def test_seed_spends_budget_on_stalest(client, monkeypatch):
    calls = []

    class FakeProvider:
        async def search_full(self, q):
            calls.append(q.label)
            return None, cards(), 12
    import app.providers.rapidapi_booking as rb
    monkeypatch.setattr(rb, "BookingRapidAPIProvider", FakeProvider)
    done = run(client, seo.seed_airports, 2)
    assert len(done) == 2 and len(calls) == 2
    assert all(s.endswith(tuple(a[0].lower() for a in seo.AIRPORTS)) for s in done)
    again = run(client, seo.seed_airports, 2)            # next stalest, not the same two
    assert not set(again) & set(done)
    assert run(client, seo.seed_airports, 0) == []


def test_stats_endpoint_falls_back_to_saved_data(client):
    st = stats_service.compute(cards(base=44), 12, days=3)
    lat, lon = 35.5 + uuid.uuid4().int % 1000 / 10000, -100.0
    run(client, seo.record_search, lat, lon, f"Fallback {uuid.uuid4().hex[:4]}", st)
    r = client.get(f"/stats?lat={lat}&lon={lon}&cache_only=true").json()
    assert r.get("cheapest_per_day") == 44
