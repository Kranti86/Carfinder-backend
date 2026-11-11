"""Clean domain models — CarFinder's OWN types.

These are the only shapes the routers, cache, and (eventually) the frontend deal
with. The upstream provider's raw JSON is translated into these by the normalizer
in app/providers/ and never leaks past it. When the upstream schema drifts, only
the normalizer changes — these models and everything above them stay stable.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# /suggest
# --------------------------------------------------------------------------- #
class SuggestionType(str, Enum):
    airport = "airport"
    city = "city"
    zip = "zip"


class Suggestion(BaseModel):
    label: str                      # "Miami International Airport"
    sublabel: str = ""              # "Miami, FL"
    type: SuggestionType
    lat: float
    lon: float
    iata: str | None = None         # only for airports


# --------------------------------------------------------------------------- #
# /search
# --------------------------------------------------------------------------- #
class SortMode(str, Enum):
    cheapest = "cheapest"      # price asc, then closest  (default)
    closest = "closest"        # distance asc, then price
    best_per_type = "best_per_type"  # collapse to the cheapest card per type


class SearchQuery(BaseModel):
    """Everything that affects results — and therefore the cache key."""

    lat: float
    lon: float
    pickup_datetime: str            # ISO 8601, snapped to 30-min in the cache key
    dropoff_datetime: str
    # One-way rentals: a distinct drop-off location is a distinct search.
    dropoff_lat: float | None = None
    dropoff_lon: float | None = None
    currency: str = "USD"
    # Two-letter country of residence — Booking V2 derives pricing/currency from
    # this, so it genuinely changes results and belongs in the cache key.
    country: str = "us"
    driver_age: int | None = None
    # Display labels only (stored on the quote / reservation) — NOT part of the cache key.
    label: str | None = None
    dropoff_label: str | None = None
    # Ordering only — deliberately NOT part of the cache key, so every sort mode
    # shares the same cached upstream fetch (ranking is applied after retrieval).
    sort: SortMode = SortMode.cheapest

    @property
    def is_one_way(self) -> bool:
        return self.dropoff_lat is not None and self.dropoff_lon is not None


class CarCard(BaseModel):
    vehicle_id: str
    name: str
    supplier: str = ""
    supplier_rating: float | None = None
    seats: int | None = None
    transmission: str | None = None
    vehicle_type: str = "Other"      # normalized class: Economy, SUV, ... (for grouping)
    # General area only (e.g. "Miami Intl · Downtown") — shown while shopping.
    # The EXACT street address is deliberately NOT exposed to the client, so a
    # customer can't bypass us and book the supplier direct. It's revealed only
    # after the reservation is placed (owner Slack + post-deposit confirmation).
    pickup_location: str = ""
    pickup_lat: float | None = None
    pickup_lon: float | None = None
    # Model A pricing: base is paid to the supplier at the counter; service_fee is
    # our platform fee, paid to us online. price = base + service_fee (the trip total).
    base_price: float | None = None      # supplier amount — payable at pick-up
    service_fee: float | None = None     # our platform fee — payable online now
    price: float | None = None           # trip total (base + service_fee)
    original_price: float | None = None  # pre-discount total "was" price, if shown
    currency: str = "USD"
    price_duration: str = ""         # e.g. "Price for 1 day"
    pay_when: str = ""               # e.g. "Pay at pick-up" / "Pay part now"
    free_cancellation: bool = False
    image: str | None = None
    # --- ranking annotations (filled by the ranking service, not the provider) ---
    distance_km: float | None = None  # from the search pickup coords
    best_in_type: bool = False        # cheapest card within its vehicle_type


class JobStatus(str, Enum):
    pending = "pending"
    done = "done"
    error = "error"


class SearchJob(BaseModel):
    """A job-based search: submit returns this immediately with status=pending."""

    job_id: str
    status: JobStatus = JobStatus.pending
    # Populated when status == done:
    search_key: str | None = None    # upstream key needed for detail lookups
    cards: list[CarCard] = Field(default_factory=list)
    cached: bool = False
    priced_at: str | None = None     # ISO timestamp — surface "prices as of X"
    # Populated when status == error:
    error: str | None = None


# --------------------------------------------------------------------------- #
# detail lookup
# --------------------------------------------------------------------------- #
class VehicleDetail(BaseModel):
    vehicle_id: str
    name: str
    supplier: str = ""
    price: float | None = None
    currency: str = "USD"
    # Raw-but-normalized extras kept loose on purpose; refine as the UI needs them.
    features: list[str] = Field(default_factory=list)
    included: list[str] = Field(default_factory=list)
    pickup_instructions: str = ""
    raw_summary: dict = Field(default_factory=dict)
