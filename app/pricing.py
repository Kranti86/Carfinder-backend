"""Price markup — applied SERVER-SIDE, so the browser can never change it.

The customer-facing price = supplier base price × (1 + markup), where the markup
percentage depends on which base-price band the rental falls into. The base is
shown to the customer as "pay at pick-up"; the markup is our platform fee, the
only part charged online. This module is the single source of truth for it, and
app/services/quotes.py re-derives every checkout amount from it.

Bands (base price from RapidAPI, USD):
    $0   – $100   → +38%
    $100 – $200   → +42%
    $200 – $350   → +38%
    $350 – $700   → +36%
    $700+         → +34%

Lower bound inclusive, upper bound exclusive (so $100.00 uses the 42% band).
"""

from __future__ import annotations

# (threshold, markup) — first band whose threshold the base is BELOW wins.
_TIERS = [
    (100, 0.38),
    (200, 0.42),
    (350, 0.38),
    (700, 0.36),
]
_TOP_MARKUP = 0.34  # base >= 700


def markup_for(base: float) -> float:
    """Return the markup fraction (e.g. 0.38) for a given base price."""
    for threshold, m in _TIERS:
        if base < threshold:
            return m
    return _TOP_MARKUP


def service_fee(base: float | None) -> float | None:
    """Our platform fee for a base rental price (the markup amount, paid to us online)."""
    if base is None:
        return None
    return round(base * markup_for(base), 2)


def apply_markup(base: float | None) -> float | None:
    """Total trip price = base + platform fee (base × (1 + markup)). None-safe."""
    if base is None:
        return None
    return round(base * (1.0 + markup_for(base)), 2)
