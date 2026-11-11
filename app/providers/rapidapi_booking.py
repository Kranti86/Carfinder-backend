"""Booking.com car rentals via RapidAPI — concrete InventoryProvider.

This module is the anti-corruption layer (review §7): it is the ONLY code that
knows the upstream's raw JSON shape. Everything else consumes CarCard /
VehicleDetail. When the upstream schema drifts (it already did once:
`search_results` → `content.items`), this file is the single place to fix, and
the normalizer is deliberately defensive about *where* fields live.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from ..circuit_breaker import CircuitBreaker
from ..config import get_settings
from ..models import CarCard, SearchQuery, VehicleDetail
from ..pricing import apply_markup, service_fee
from . import airports
from .http import UpstreamError, request_json


def _norm_name(name: str) -> str:
    """Normalize a location name for tolerant matching (case + whitespace)."""
    return " ".join(str(name).split()).casefold()

logger = logging.getLogger("carfinder.booking")

_BREAKER = CircuitBreaker("booking")


class ListingExpiredError(RuntimeError):
    """The search_key expired and a refreshed retry still failed — the caller
    should prompt the user to search again."""


def _decode_search_key(key: str) -> dict | None:
    """The search_key is a base64-encoded JSON blob (params + signature). Decode it
    so we can reconstruct the original search and mint a fresh key when it expires.

    Isolated here in the anti-corruption layer; if the encoding ever changes, this
    is the only place to touch."""
    if not key:
        return None
    try:
        padded = key + "=" * (-len(key) % 4)
        raw = base64.b64decode(padded)
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _headers() -> dict[str, str]:
    s = get_settings()
    return {"x-rapidapi-key": s.rapidapi_key, "x-rapidapi-host": s.rapidapi_host}


def _split_dt(iso: str) -> tuple[str, str]:
    """'2026-08-01T10:30' -> ('2026-08-01', '10:30'). Time defaults to 10:00."""
    if "T" in iso:
        date_part, time_part = iso.split("T", 1)
        return date_part, time_part[:5] or "10:00"
    return iso, "10:00"


# --------------------------------------------------------------------------- #
# Defensive field extraction — tolerate schema drift.
# --------------------------------------------------------------------------- #
def _first(d: dict, *paths: str) -> Any:
    """Return the first present value among dotted paths, else None."""
    for path in paths:
        cur: Any = d
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _extract_items(payload: dict) -> list[dict]:
    """Return only the CAR_CARD entries from the response.

    Current shape (verified against a live response): cards live in
    data.content.items, each wrapped as {type, positionInList, content}. The list
    is interleaved with non-car entries (RESULTS_COUNT, SHELL_REGION_VIEW, …) that
    must be filtered out. data.search_results is now returned EMPTY — we fall back
    to it only if it ever carries data again.
    """
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []

    content = data.get("content")
    items = content.get("items") if isinstance(content, dict) else None
    if isinstance(items, list) and items:
        cards = [it for it in items if it.get("type") == "CAR_CARD"]
        if cards:
            return cards

    # Legacy fallback: a non-empty flat list under search_results/results.
    for candidate in ("search_results", "results"):
        val = data.get(candidate)
        if isinstance(val, list) and val:
            return val

    logger.warning(
        "no CAR_CARD items found; data keys=%s content keys=%s",
        list(data.keys()),
        list(content.keys()) if isinstance(content, dict) else None,
    )
    return []


def _extract_search_key(payload: dict) -> str | None:
    return _first(
        payload,
        "data.search_key",
        "data.search_context.searchKey",
        "search_key",
        "searchKey",
    )


def _extract_currency(payload: dict) -> str | None:
    return _first(payload, "data.metadata.lowestVehiclePrice.currency", "data.currency")


def _extract_total_count(payload: dict) -> int | None:
    """Total result count for the search (e.g. 2515), from the RESULTS_COUNT item's
    label ("2515 results"). Used for SEO stats ("N cars available")."""
    data = payload.get("data", payload)
    content = data.get("content") if isinstance(data, dict) else None
    items = content.get("items") if isinstance(content, dict) else None
    if isinstance(items, list):
        for it in items:
            if it.get("type") == "RESULTS_COUNT":
                label = str(_first(it, "content.label") or "")
                digits = "".join(ch for ch in label if ch.isdigit())
                if digits:
                    return int(digits)
    return None


def build_location_index(payload: dict) -> dict[str, dict]:
    """Map pickup location name -> {lat, lon, address} from data.map.supplierLocations.

    Cards carry only a location *name*; the coordinates AND the full street address
    live here, keyed by the same name. Joining the two gives us distance ranking and
    the complete pickup address to show the customer.
    """
    index: dict[str, dict] = {}
    locations = _first(payload, "data.map.supplierLocations") or []
    if not isinstance(locations, list):
        return index
    for loc in locations:
        name = _first(loc, "metaData.locationName", "carouselContent.title")
        lat = _to_float(_first(loc, "supplierPinContent.latLong.latitude"))
        lon = _to_float(_first(loc, "supplierPinContent.latLong.longitude"))
        address = str(_first(loc, "carouselContent.body") or "")
        if name and lat is not None and lon is not None:
            entry = {"lat": lat, "lon": lon, "address": address}
            index[str(name)] = entry
            index[_norm_name(name)] = entry  # tolerant lookup key
    return index


def _normalize_card(
    item: dict,
    *,
    currency: str,
    location_index: dict[str, tuple[float, float]] | None = None,
) -> CarCard | None:
    """Map one CAR_CARD entry to a clean CarCard. `item` is the outer wrapper
    ({type, content}); all the useful data lives under `content`."""
    content = item.get("content", item)
    vehicle_id = _first(content, "metadata.vehicleId", "vehicleId", "vehicle_id")
    if vehicle_id is None:
        return None

    pickup_location = str(_first(content, "location.pickup.location") or "")
    # Coordinates only (for distance ranking). We deliberately DON'T attach the
    # exact street address to the client-facing card — see CarCard.pickup_location.
    # The map block's full address stays server-side for reservation fulfilment.
    idx = location_index or {}
    entry = idx.get(pickup_location) or idx.get(_norm_name(pickup_location))
    if entry:
        p_lat, p_lon = entry["lat"], entry["lon"]
    else:
        coords = airports.coords_for_name(pickup_location) if pickup_location else None
        p_lat, p_lon = (coords if coords else (None, None))

    # Prices are marked up SERVER-SIDE; the client never sees the base supplier price.
    base_price = _to_float(_first(content, "pricing.finalPriceDisplay"))
    base_original = _to_float(_first(content, "pricing.originalPriceDisplay"))

    return CarCard(
        vehicle_id=str(vehicle_id),
        name=str(_first(content, "title") or "Unknown vehicle").strip(),
        supplier=str(_first(content, "supplier.name") or ""),
        supplier_rating=_to_float(_first(content, "supplier.rating.score")),
        seats=_parse_seats(_first(content, "specs")),
        transmission=_parse_transmission(content.get("vehicleSpecs")),
        vehicle_type=_type_from_subtitle(_first(content, "subtitle")),
        pickup_location=pickup_location,
        pickup_lat=p_lat,
        pickup_lon=p_lon,
        base_price=base_price,                    # supplier amount (pay at counter)
        service_fee=service_fee(base_price),      # our platform fee (pay online)
        price=apply_markup(base_price),           # trip total = base + fee
        original_price=apply_markup(base_original),
        currency=currency,
        price_duration=str(_first(content, "pricing.durationText") or ""),
        pay_when=str(_first(content, "payWhen") or ""),
        free_cancellation=_has_free_cancellation(content.get("badges")),
        image=_first(content, "imageUrl", "image_url", "image"),
    )


# The upstream expresses class as a subtitle like "or similar large car" / "SUV".
# Map those onto the same buckets the response's own car-type filter uses.
_SUBTITLE_TYPES = [
    ("small car", "Compact"),
    ("mini car", "Compact"),
    ("economy", "Compact"),
    ("compact", "Compact"),
    ("medium car", "Mid-size"),
    ("mid-size", "Mid-size"),
    ("midsize", "Mid-size"),
    ("intermediate", "Mid-size"),
    ("large car", "Full-size"),
    ("full-size", "Full-size"),
    ("fullsize", "Full-size"),
    ("standard", "Full-size"),
    ("station wagon", "Estate"),
    ("estate", "Estate"),
    ("wagon", "Estate"),
    ("minivan", "Minivan"),
    ("people carrier", "Minivan"),
    ("passenger van", "Van"),
    ("cargo van", "Van"),
    ("commercial", "Van"),
    ("van", "Van"),
    ("luxury", "Luxury"),
    ("premium", "Luxury"),
    ("executive", "Luxury"),
    ("sports car", "Sports"),
    ("coupe", "Sports"),
    ("convertible", "Convertible"),
    ("pickup", "Pickup"),
    ("truck", "Pickup"),
    ("suv", "SUV"),  # checked last: "SUV" as a whole word, after multi-word classes
]


def _type_from_subtitle(subtitle: Any) -> str:
    if not subtitle:
        return "Other"
    text = str(subtitle).lower().replace("or similar", "").strip()
    for needle, label in _SUBTITLE_TYPES:
        if needle in text:
            return label
    return "Other"


def _parse_seats(specs: Any) -> int | None:
    """'5 seats | 4 doors' -> 5."""
    if not specs:
        return None
    for token in str(specs).split("|"):
        token = token.strip().lower()
        if "seat" in token:
            num = "".join(ch for ch in token if ch.isdigit())
            return int(num) if num else None
    return None


def _parse_transmission(vehicle_specs: Any) -> str | None:
    """Pull the transmission label from the vehicleSpecs list (icon TRANSMISSION_*)."""
    if not isinstance(vehicle_specs, list):
        return None
    for spec in vehicle_specs:
        icon = str(spec.get("icon", "")).upper()
        if icon.startswith("TRANSMISSION"):
            return spec.get("text")
    return None


def _has_free_cancellation(badges: Any) -> bool:
    if not isinstance(badges, list):
        return False
    return any("free-cancellation" in str(b.get("id", "")).lower() for b in badges)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class BookingRapidAPIProvider:
    name = "booking-rapidapi"

    def __init__(self) -> None:
        self._settings = get_settings()

    def _url(self, path: str) -> str:
        return f"{self._settings.rapidapi_base_url.rstrip('/')}/{path.lstrip('/')}"

    async def search(self, query: SearchQuery) -> tuple[str | None, list[CarCard]]:
        key, cards, _count = await self.search_full(query)
        return key, cards

    async def search_full(
        self, query: SearchQuery
    ) -> tuple[str | None, list[CarCard], int | None]:
        if not self._settings.rapidapi_enabled:
            raise RuntimeError("RAPIDAPI_KEY is not configured")

        pu_date, pu_time = _split_dt(query.pickup_datetime)
        do_date, do_time = _split_dt(query.dropoff_datetime)
        drop_lat = query.dropoff_lat if query.is_one_way else query.lat
        drop_lon = query.dropoff_lon if query.is_one_way else query.lon

        # Param names match the RapidAPI "Car Rental - V2" searchCarRentals surface
        # (snake_case here; note vehicleDetails below is camelCase). Currency is not
        # a param on V2 — it's derived from countryOfResidence.
        params = {
            "pick_up_latitude": query.lat,
            "pick_up_longitude": query.lon,
            "drop_off_latitude": drop_lat,
            "drop_off_longitude": drop_lon,
            "pick_up_date": pu_date,       # YYYY-MM-DD
            "pick_up_time": pu_time,       # HH:MM
            "drop_off_date": do_date,
            "drop_off_time": do_time,
            "countryOfResidence": query.country or self._settings.default_country_of_residence,
        }
        if query.driver_age is not None:
            params["driver_age"] = query.driver_age

        payload = await request_json(
            "GET",
            self._url("/api/v2/cars/searchCarRentals"),
            breaker=_BREAKER,
            params=params,
            headers=_headers(),
        )

        search_key = _extract_search_key(payload)
        currency = _extract_currency(payload) or query.currency or self._settings.default_currency
        location_index = build_location_index(payload)
        items = _extract_items(payload)
        cards = [
            c for c in (
                _normalize_card(it, currency=currency, location_index=location_index)
                for it in items
            ) if c is not None
        ]
        located = sum(1 for c in cards if c.pickup_lat is not None)
        total_count = _extract_total_count(payload)
        logger.info(
            "search normalized %d/%d cards (%d with coords, currency=%s, total=%s)",
            len(cards), len(items), located, currency, total_count,
        )
        return search_key, cards, total_count

    async def refresh_search_key(self, stale_key: str) -> str | None:
        """Reconstruct the original search from a (possibly expired) key and re-run
        it to obtain a fresh key. Returns None if the key can't be decoded."""
        decoded = _decode_search_key(stale_key)
        if not decoded:
            return None
        loc = str(decoded.get("pickUpLocation", "")).split(",")
        if len(loc) != 2:
            return None
        try:
            lat, lon = float(loc[0]), float(loc[1])
        except ValueError:
            return None
        query = SearchQuery(
            lat=lat,
            lon=lon,
            pickup_datetime=str(decoded.get("pickUpDateTime", "")),
            dropoff_datetime=str(decoded.get("dropOffDateTime", "")),
            driver_age=decoded.get("driversAge"),
            country=self._settings.default_country_of_residence,
        )
        fresh_key, _cards = await self.search(query)
        return fresh_key

    async def _detail_json(
        self, path: str, search_key: str, vehicle_id: str, extra: dict | None = None
    ) -> dict:
        """GET a detail endpoint, transparently refreshing an expired search_key and
        retrying once. Raises ListingExpiredError if the retry also fails."""
        if not self._settings.rapidapi_enabled:
            raise RuntimeError("RAPIDAPI_KEY is not configured")

        params = {"searchKey": search_key, "vehicleId": vehicle_id, **(extra or {})}
        try:
            return await request_json(
                "GET", self._url(path), breaker=_BREAKER, params=params, headers=_headers()
            )
        except UpstreamError:
            fresh = await self.refresh_search_key(search_key)
            if not fresh or fresh == search_key:
                raise
            logger.info("search_key expired; refreshed and retrying %s", path)
            params["searchKey"] = fresh
            try:
                return await request_json(
                    "GET", self._url(path), breaker=_BREAKER, params=params, headers=_headers()
                )
            except UpstreamError as exc:
                raise ListingExpiredError("listing expired; please search again") from exc

    async def vehicle_detail(self, search_key: str, vehicle_id: str) -> VehicleDetail:
        # vehicleDetails uses camelCase params (searchKey / vehicleId).
        payload = await self._detail_json("/api/v2/cars/vehicleDetails", search_key, vehicle_id)
        data = payload.get("data", payload)
        return VehicleDetail(
            vehicle_id=str(vehicle_id),
            name=str(_first(data, "vehicle_info.v_name", "vehicle_info.name", "name") or "Unknown vehicle"),
            supplier=str(_first(data, "supplier_info.name", "supplier.name") or ""),
            price=_to_float(_first(data, "pricing_info.price", "price.amount", "price")),
            currency=str(_first(data, "pricing_info.currency", "currency") or self._settings.default_currency),
            features=_as_list(_first(data, "vehicle_info.features", "features")),
            included=_as_list(_first(data, "content.included", "included")),
            pickup_instructions=str(_first(data, "route_info.pickup.instructions", "pickup_instructions") or ""),
            raw_summary={},
        )


    async def supplier_details(
        self, search_key: str, vehicle_id: str, tab: str = "details"
    ) -> dict:
        """Raw-but-unwrapped supplier info. Shape isn't modeled yet — the frontend
        renders it directly; wrap in a typed model once the UI needs specific fields."""
        payload = await self._detail_json(
            "/api/v2/cars/supplierDetails",
            search_key,
            vehicle_id,
            extra={
                "tab": tab,
                "countryOfResidence": self._settings.default_country_of_residence,
            },
        )
        return payload.get("data", payload)

    async def booking_summary(self, search_key: str, vehicle_id: str) -> dict:
        """Raw-but-unwrapped booking summary (price breakdown, terms). Read-only —
        the upstream cannot actually make a reservation (architecture doc §7)."""
        payload = await self._detail_json(
            "/api/v2/cars/bookingSummary",
            search_key,
            vehicle_id,
            extra={"countryOfResidence": self._settings.default_country_of_residence},
        )
        return payload.get("data", payload)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
