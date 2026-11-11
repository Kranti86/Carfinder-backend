"""Ranking: cheapest first, then closest, with best-in-type flagged.

Pickup origin for these tests is Miami International (25.7959, -80.2870).
"""

from app.models import CarCard, SortMode
from app.services.ranking import rank

_ORIGIN = dict(origin_lat=25.7959, origin_lon=-80.2870)


def _card(vid, price, vtype="Economy", lat=None, lon=None):
    return CarCard(
        vehicle_id=vid, name=vid, vehicle_type=vtype,
        price=price, pickup_lat=lat, pickup_lon=lon,
    )


def test_cheapest_first():
    cards = [_card("a", 300), _card("b", 150), _card("c", 220)]
    ordered = rank(cards, mode=SortMode.cheapest, **_ORIGIN)
    assert [c.vehicle_id for c in ordered] == ["b", "c", "a"]


def test_ties_broken_by_closest():
    # same price; b is at the airport, a is ~15km away -> b (closer) wins
    a = _card("a", 200, lat=25.90, lon=-80.30)
    b = _card("b", 200, lat=25.7959, lon=-80.2870)
    ordered = rank([a, b], mode=SortMode.cheapest, **_ORIGIN)
    assert [c.vehicle_id for c in ordered] == ["b", "a"]
    assert ordered[0].distance_km < ordered[1].distance_km


def test_missing_price_sorts_last():
    cards = [_card("a", None), _card("b", 100)]
    ordered = rank(cards, mode=SortMode.cheapest, **_ORIGIN)
    assert [c.vehicle_id for c in ordered] == ["b", "a"]


def test_best_in_type_flags_cheapest_per_type():
    cards = [
        _card("eco_hi", 200, "Economy"),
        _card("eco_lo", 120, "Economy"),
        _card("suv_lo", 350, "SUV"),
        _card("suv_hi", 500, "SUV"),
    ]
    ordered = rank(cards, mode=SortMode.cheapest, **_ORIGIN)
    flagged = {c.vehicle_id for c in ordered if c.best_in_type}
    assert flagged == {"eco_lo", "suv_lo"}


def test_best_per_type_collapses_to_one_card_each():
    cards = [
        _card("eco_hi", 200, "Economy"),
        _card("eco_lo", 120, "Economy"),
        _card("suv_lo", 350, "SUV"),
    ]
    ordered = rank(cards, mode=SortMode.best_per_type, **_ORIGIN)
    assert [c.vehicle_id for c in ordered] == ["eco_lo", "suv_lo"]  # cheapest per type, cheapest-first


def test_closest_mode_orders_by_distance():
    near = _card("near", 400, lat=25.7959, lon=-80.2870)
    far = _card("far", 100, lat=26.20, lon=-80.10)
    ordered = rank([far, near], mode=SortMode.closest, **_ORIGIN)
    assert [c.vehicle_id for c in ordered] == ["near", "far"]


def test_distance_is_annotated():
    ordered = rank([_card("a", 100, lat=25.80, lon=-80.29)], **_ORIGIN, mode=SortMode.cheapest)
    assert ordered[0].distance_km is not None and ordered[0].distance_km > 0
