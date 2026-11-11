"""City stats aggregation for the SEO generator."""

from app.models import CarCard
from app.providers.rapidapi_booking import _extract_total_count
from app.services.stats_service import compute


def _c(price, vtype, supplier, pickup="Miami Intl"):
    return CarCard(vehicle_id="x", name="n", price=price, vehicle_type=vtype,
                   supplier=supplier, pickup_location=pickup)


def test_compute_aggregates():
    cards = [
        _c(75, "Full-size", "Avis"),
        _c(76, "Compact", "Budget"),
        _c(84, "SUV", "Avis"),
        _c(110, "Minivan", "Enterprise"),
    ]
    s = compute(cards, total_count=412, days=3)
    assert s["car_count"] == 412
    assert s["cheapest_total"] == 75
    assert s["cheapest_per_day"] == 25          # 75/3
    assert s["supplier_count"] == 3              # Avis, Budget, Enterprise
    assert s["avg_by_type"]["SUV"] == 28         # 84/3
    assert "Miami Intl" in s["top_pickups"]


def test_compute_handles_no_prices():
    s = compute([CarCard(vehicle_id="x", name="n", price=None)], total_count=None)
    assert s["cheapest_total"] is None
    assert s["car_count"] == 1


def test_total_count_extraction():
    payload = {"data": {"content": {"items": [
        {"type": "RESULTS_COUNT", "content": {"label": "2515 results"}},
        {"type": "CAR_CARD", "content": {}},
    ]}}}
    assert _extract_total_count(payload) == 2515
