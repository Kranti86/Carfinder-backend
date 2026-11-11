"""Result ranking — cheapest first, then closest, with best-in-type flagging.

Applied AFTER the upstream fetch and cache read, so it's a pure in-memory sort
that adds no upstream latency and lets every sort mode share one cached fetch.

Ordering:
  - cheapest      (default): price ascending, ties broken by nearest pickup
  - closest                : nearest pickup ascending, ties broken by price
  - best_per_type          : collapse to the single cheapest card per vehicle type

In every mode we compute each card's distance from the search pickup coords and
flag the cheapest card within each vehicle_type as best_in_type=True, so the UI
can surface "best Economy / best SUV / …" without dropping the rest.

Missing values sort last: a card with no price (or no pickup coords) never
outranks one that has them.
"""

from __future__ import annotations

from ..models import CarCard, SortMode
from ..utils import haversine_km

_INF = float("inf")


def _price_key(card: CarCard) -> float:
    return card.price if card.price is not None else _INF


def _dist_key(card: CarCard) -> float:
    return card.distance_km if card.distance_km is not None else _INF


def _annotate_distance(cards: list[CarCard], lat: float, lon: float) -> None:
    for card in cards:
        if card.pickup_lat is not None and card.pickup_lon is not None:
            card.distance_km = round(haversine_km(lat, lon, card.pickup_lat, card.pickup_lon), 3)
        else:
            card.distance_km = None


def _flag_best_in_type(cards: list[CarCard]) -> None:
    cheapest_by_type: dict[str, CarCard] = {}
    for card in cards:
        card.best_in_type = False  # reset (ranking may run more than once)
        current = cheapest_by_type.get(card.vehicle_type)
        if current is None or _price_key(card) < _price_key(current):
            cheapest_by_type[card.vehicle_type] = card
    for card in cheapest_by_type.values():
        if card.price is not None:  # don't flag a "best" that has no price
            card.best_in_type = True


def rank(
    cards: list[CarCard],
    *,
    origin_lat: float,
    origin_lon: float,
    mode: SortMode = SortMode.cheapest,
) -> list[CarCard]:
    """Return a new, ordered list of cards annotated with distance + best_in_type."""
    if not cards:
        return []

    _annotate_distance(cards, origin_lat, origin_lon)
    _flag_best_in_type(cards)

    if mode is SortMode.closest:
        ordered = sorted(cards, key=lambda c: (_dist_key(c), _price_key(c)))
    elif mode is SortMode.best_per_type:
        # one card per type — the cheapest — then cheapest-first across types
        best = [c for c in cards if c.best_in_type]
        ordered = sorted(best, key=lambda c: (_price_key(c), _dist_key(c)))
    else:  # cheapest (default): price asc, then closest
        ordered = sorted(cards, key=lambda c: (_price_key(c), _dist_key(c)))

    return ordered
