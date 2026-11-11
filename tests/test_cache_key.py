"""The cache key must encode EVERY result-affecting dimension (review §1).

These tests lock in the property that genuinely-different searches never collide,
while near-identical ones (sub-100m, sub-30min) intentionally share a key.
"""

from app.cache import search_key
from app.models import SearchQuery


def _q(**kw) -> SearchQuery:
    base = dict(
        lat=25.7959, lon=-80.2870,
        pickup_datetime="2026-08-01T10:15",
        dropoff_datetime="2026-08-04T10:15",
        currency="USD",
    )
    base.update(kw)
    return SearchQuery(**base)


def test_near_identical_searches_share_a_key():
    # within ~100m and within the same 30-min slot -> same key (shared cache hit)
    a = search_key(_q(lat=25.79590, lon=-80.28700, pickup_datetime="2026-08-01T10:05"))
    b = search_key(_q(lat=25.79598, lon=-80.28702, pickup_datetime="2026-08-01T10:25"))
    assert a == b


def test_currency_changes_the_key():
    assert search_key(_q(currency="USD")) != search_key(_q(currency="EUR"))


def test_one_way_dropoff_changes_the_key():
    round_trip = search_key(_q())
    one_way = search_key(_q(dropoff_lat=26.0742, dropoff_lon=-80.1506))
    assert round_trip != one_way


def test_driver_age_changes_the_key():
    assert search_key(_q(driver_age=25)) != search_key(_q(driver_age=70))


def test_different_half_hour_slots_differ():
    a = search_key(_q(pickup_datetime="2026-08-01T10:15"))
    b = search_key(_q(pickup_datetime="2026-08-01T10:45"))
    assert a != b


def test_key_is_versioned():
    assert search_key(_q()).startswith("search:v1:")
