"""Inventory provider interface.

The search service depends on this Protocol, not on any concrete provider. To
swap Booking/RapidAPI for another source, implement these two methods and return
the same clean domain models — nothing upstream changes.
"""

from __future__ import annotations

from typing import Protocol

from ..models import CarCard, SearchQuery, VehicleDetail


class InventoryProvider(Protocol):
    name: str

    async def search(self, query: SearchQuery) -> tuple[str | None, list[CarCard]]:
        """Return (search_key, cards). search_key is passed back for detail lookups."""
        ...

    async def vehicle_detail(self, search_key: str, vehicle_id: str) -> VehicleDetail:
        ...
