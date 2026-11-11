"""Tiered price markup — the customer price is base × (1 + band markup)."""

import pytest

from app.pricing import apply_markup, markup_for, service_fee


@pytest.mark.parametrize("base,expected_markup", [
    (0, 0.38), (50, 0.38), (99.99, 0.38),
    (100, 0.42), (150, 0.42), (199.99, 0.42),
    (200, 0.38), (349.99, 0.38),
    (350, 0.36), (699.99, 0.36),
    (700, 0.34), (1500, 0.34),
])
def test_band_markups(base, expected_markup):
    assert markup_for(base) == expected_markup


def test_apply_markup_values():
    assert apply_markup(54) == 74.52      # 54 * 1.38
    assert apply_markup(150) == 213.0     # 150 * 1.42
    assert apply_markup(300) == 414.0     # 300 * 1.38
    assert apply_markup(500) == 680.0     # 500 * 1.36
    assert apply_markup(1000) == 1340.0   # 1000 * 1.34


def test_service_fee_is_markup_amount():
    assert service_fee(54) == 20.52       # 54 * 0.38
    assert service_fee(150) == 63.0       # 150 * 0.42
    # base + fee == total
    assert round(54 + service_fee(54), 2) == apply_markup(54)


def test_apply_markup_none():
    assert apply_markup(None) is None
    assert service_fee(None) is None


def test_boundaries_use_upper_band():
    # exactly on a boundary uses the new (higher-threshold) band
    assert markup_for(100) == 0.42
    assert markup_for(200) == 0.38
