"""Server-side quotes — the ONLY place an online amount is decided.

When a search job finishes we snapshot every card's server-computed price
(base + platform fee) together with the search dates, keyed by job id. Checkout
then refers to a car by (job_id, vehicle_id) and the server works out:

    service_fee  = the card's markup (0 when the admin fee toggle is off)
    insurance    = cdw_net_per_day × days × (1 + markup)   (only if chosen)
    pay_now      = service_fee + insurance

The browser never sends an amount that we trust. PayPal orders are bound to the
quote they were created for (see `remember_order`), so POST /requests can check
that the approved order is for exactly the amount we computed.
"""

from __future__ import annotations

import math
from datetime import datetime

from ..cache import get_cache
from ..config import get_settings
from ..models import CarCard, SearchQuery
from . import settings_service

_QUOTE_NS = "quote:v1"
_ORDER_NS = "paypalorder:v1"


def _quote_key(job_id: str) -> str:
    return f"{_QUOTE_NS}:{job_id}"


def _order_key(order_id: str) -> str:
    return f"{_ORDER_NS}:{order_id}"


def trip_days(pickup: str, dropoff: str) -> int:
    """Whole rental days, rounded up, minimum 1 (same rule the site displays)."""
    try:
        a = datetime.fromisoformat(pickup[:16])
        b = datetime.fromisoformat(dropoff[:16])
    except (TypeError, ValueError):
        return 1
    days = math.ceil((b - a).total_seconds() / 86400)
    return days if days > 0 else 1


def insurance_for(days: int) -> dict | None:
    s = get_settings()
    if not s.insurance_enabled:
        return None
    net = round(s.insurance_cdw_net_per_day * days, 2)
    price = round(net * (1 + s.insurance_markup), 2)
    return {"label": s.insurance_label, "days": days, "net": net, "price": price}


def insurance_price_per_day() -> float:
    s = get_settings()
    return round(s.insurance_cdw_net_per_day * (1 + s.insurance_markup), 2)


async def store_job_quote(job_id: str, query: SearchQuery, cards: list[CarCard]) -> None:
    """Snapshot the priced cards of a finished search so checkout can be priced later."""
    data = {
        "search": {
            "label": query.label or "",
            "dropoff_label": query.dropoff_label or "",
            "lat": query.lat, "lon": query.lon,
            "pickup": query.pickup_datetime, "dropoff": query.dropoff_datetime,
        },
        "cards": {
            c.vehicle_id: {
                "name": c.name, "supplier": c.supplier, "vehicle_type": c.vehicle_type,
                "pickup_location": c.pickup_location, "currency": c.currency or "USD",
                "base_price": c.base_price, "service_fee": c.service_fee,
            }
            for c in cards
        },
    }
    await get_cache().set_json(_quote_key(job_id), data, get_settings().quote_ttl_seconds)


async def compute(job_id: str, vehicle_id: str, insurance: bool) -> dict | None:
    """Authoritative price for one car of one search. None if the quote expired/unknown."""
    if not job_id or not vehicle_id:
        return None
    data = await get_cache().get_json(_quote_key(str(job_id)))
    if not data:
        return None
    card = (data.get("cards") or {}).get(str(vehicle_id))
    if not card or card.get("base_price") is None:
        return None
    search = data.get("search") or {}
    days = trip_days(search.get("pickup", ""), search.get("dropoff", ""))
    fees_on = await settings_service.service_fee_enabled()
    fee = round(float(card.get("service_fee") or 0.0), 2) if fees_on else 0.0
    offer = insurance_for(days)
    ins = offer if (insurance and offer) else None
    pay_now = round(fee + (ins["price"] if ins else 0.0), 2)
    return {
        "job_id": str(job_id), "vehicle_id": str(vehicle_id),
        "fees_on": fees_on,
        "base_price": round(float(card["base_price"]), 2),
        "service_fee": fee,
        "insurance": ins,                 # includes `net` — internal only
        "insurance_offer": offer,         # what the customer may add
        "pay_now": pay_now,
        "currency": card.get("currency") or "USD",
        "vehicle": card, "search": search, "days": days,
    }


def public_quote(q: dict) -> dict:
    """Customer-safe view of a quote (no insurer net, no internals)."""
    offer = q.get("insurance_offer")
    return {
        "job_id": q["job_id"], "vehicle_id": q["vehicle_id"], "fees_on": q["fees_on"],
        "currency": q["currency"], "days": q["days"],
        "base_price": q["base_price"], "service_fee": q["service_fee"],
        "insurance": ({"label": offer["label"], "days": offer["days"], "price": offer["price"]}
                      if offer else None),
        "pay_now_without_insurance": q["service_fee"],
        "pay_now_with_insurance": (round(q["service_fee"] + offer["price"], 2)
                                   if offer else q["service_fee"]),
    }


async def remember_order(order_id: str, *, reservation_id: str, job_id: str,
                         vehicle_id: str, insurance: bool, amount: float) -> None:
    await get_cache().set_json(_order_key(order_id), {
        "reservation_id": reservation_id, "job_id": job_id, "vehicle_id": vehicle_id,
        "insurance": bool(insurance), "amount": amount,
    }, get_settings().order_ttl_seconds)


async def get_order(order_id: str) -> dict | None:
    if not order_id:
        return None
    return await get_cache().get_json(_order_key(str(order_id)))
