"""Airport IATA enrichment.

Mapbox doesn't return IATA codes, but codes are the stable identifier for airports
(coordinates drift). We match a geocoder airport result to the nearest known
airport in data/airports.csv and attach its IATA code.

The CSV is loaded once at import. See data/README for how to regenerate the full
US subset from the OpenFlights dataset (review §9: ship the full set, don't grow a
hand-maintained inline list).
"""

from __future__ import annotations

import csv
import logging
import math
from pathlib import Path

logger = logging.getLogger("carfinder.airports")

_DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "airports.csv"


class _Airport:
    __slots__ = ("iata", "name", "city", "region", "lat", "lon")

    def __init__(self, iata: str, name: str, city: str, region: str, lat: float, lon: float):
        self.iata = iata
        self.name = name
        self.city = city
        self.region = region
        self.lat = lat
        self.lon = lon


def _load() -> list[_Airport]:
    airports: list[_Airport] = []
    if not _DATA_FILE.exists():
        logger.warning("airports.csv not found at %s — IATA enrichment disabled", _DATA_FILE)
        return airports
    with _DATA_FILE.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                airports.append(
                    _Airport(
                        iata=row["iata"].strip().upper(),
                        name=row["name"].strip(),
                        city=row.get("city", "").strip(),
                        region=row.get("region", "").strip(),
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                    )
                )
            except (KeyError, ValueError):
                continue
    logger.info("loaded %d airports for IATA enrichment", len(airports))
    return airports


_AIRPORTS: list[_Airport] = _load()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def match_iata(lat: float, lon: float, *, max_km: float = 8.0) -> str | None:
    """Return the IATA code of the nearest known airport within max_km, else None."""
    best: tuple[float, str] | None = None
    for ap in _AIRPORTS:
        d = _haversine_km(lat, lon, ap.lat, ap.lon)
        if d <= max_km and (best is None or d < best[0]):
            best = (d, ap.iata)
    return best[1] if best else None


def coords_for_name(location_name: str | None) -> tuple[float, float] | None:
    """Best-effort coordinates for a pickup location that names an airport.

    Booking only publishes coordinates for a curated subset of pickup locations,
    so airport depots (e.g. "LaGuardia Airport") often arrive without coords. When
    the location text names a known airport, fall back to that airport's position
    so distance ranking still works for airport rentals."""
    if not location_name:
        return None
    text = location_name.lower()
    for ap in _AIRPORTS:
        name = ap.name.lower()
        if name and name in text:
            return (ap.lat, ap.lon)
        if ap.city and f"{ap.city.lower()} airport" in text:
            return (ap.lat, ap.lon)
    return None


def count() -> int:
    return len(_AIRPORTS)
