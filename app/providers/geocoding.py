"""Mapbox geocoding provider for /suggest.

Turns partial user text into ranked location suggestions. Handles ZIPs, cities,
and airports in one call. POIs are dropped unless they're airports; results are
ranked airports → cities → ZIPs (rental demand is airport-heavy). Airports get an
IATA code attached from the local table.

This module is the ONLY place that knows Mapbox's response shape.
"""

from __future__ import annotations

import logging

from ..circuit_breaker import CircuitBreaker
from ..config import get_settings
from ..models import Suggestion, SuggestionType
from . import airports
from .http import request_json

logger = logging.getLogger("carfinder.geocoding")

_BREAKER = CircuitBreaker("mapbox")
_MAPBOX_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places/{query}.json"

_TYPE_RANK = {SuggestionType.airport: 0, SuggestionType.city: 1, SuggestionType.zip: 2}


def _classify(feature: dict) -> SuggestionType | None:
    """Map a Mapbox feature to our SuggestionType, or None to drop it."""
    place_types = feature.get("place_type", [])
    if "postcode" in place_types:
        return SuggestionType.zip
    if "place" in place_types or "locality" in place_types:
        return SuggestionType.city
    if "poi" in place_types:
        # Keep POIs only if they look like an airport.
        categories = (feature.get("properties", {}).get("category") or "").lower()
        text = feature.get("text", "").lower()
        if "airport" in categories or "airport" in text:
            return SuggestionType.airport
    return None


def _to_suggestion(feature: dict, stype: SuggestionType) -> Suggestion | None:
    center = feature.get("center") or []
    if len(center) != 2:
        return None
    lon, lat = float(center[0]), float(center[1])
    label = feature.get("text") or feature.get("place_name", "")
    # sublabel = the context trail minus the label itself (e.g. "Miami, Florida")
    context = feature.get("context", [])
    sublabel = ", ".join(c.get("text", "") for c in context[:2] if c.get("text"))

    iata = airports.match_iata(lat, lon) if stype is SuggestionType.airport else None
    return Suggestion(
        label=label,
        sublabel=sublabel,
        type=stype,
        lat=lat,
        lon=lon,
        iata=iata,
    )


async def suggest(query: str, *, limit: int = 8, country: str = "us") -> list[Suggestion]:
    """Return ranked location suggestions for a partial query (>= 3 chars)."""
    settings = get_settings()
    if not settings.mapbox_enabled:
        raise RuntimeError("MAPBOX_TOKEN is not configured")

    params = {
        "access_token": settings.mapbox_token,
        "autocomplete": "true",
        "country": country,
        "types": "postcode,place,locality,poi",
        "limit": str(limit),
    }
    data = await request_json(
        "GET",
        _MAPBOX_URL.format(query=query.strip()),
        breaker=_BREAKER,
        params=params,
    )

    suggestions: list[Suggestion] = []
    for feature in data.get("features", []):
        stype = _classify(feature)
        if stype is None:
            continue
        s = _to_suggestion(feature, stype)
        if s is not None:
            suggestions.append(s)

    suggestions.sort(key=lambda s: _TYPE_RANK.get(s.type, 9))
    return suggestions
