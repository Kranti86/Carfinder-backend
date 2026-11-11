"""Google Ads Customer Match lists built from reservations.

Each segment is a CSV in Google's "hashed data file" format with every identifier
Google can match on: Email, Phone, First Name, Last Name (normalised + SHA-256),
plus Country and Zip (sent in clear, as Google requires). More identifiers per
person = a higher match rate, so we send all four groups whenever we have them.
No raw email, phone or name ever leaves the server.

Upload a segment in Google Ads → Tools → Audience manager → + → Customer list →
"Upload hashed data". Use "Replace all data" when refreshing a list.

Who is left out, always:
  * visitors who clicked "Reject" on the cookie banner (ads_opt_out),
  * obvious test bookings (example.com / test domains, names starting "TEST").
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..models_db import Reservation

SEGMENTS: dict[str, str] = {
    "customers": "Confirmed customers: paid, confirmed or completed rentals",
    "high-value": "Top 20% of customers by lifetime booking value (lookalike / bid-up seed)",
    "repeat-customers": "Customers with 2 or more confirmed rentals",
    "recent-customers": "Customers with a confirmed rental in the last 90 days",
    "lapsed-customers": "Past customers with no rental in 180+ days (win-back)",
    "insurance-buyers": "Confirmed customers who bought insurance",
    "unconverted": "Started a reservation but never paid (re-engage)",
    "cancelled": "Reservations that were cancelled, refunded or voided",
    "all-bookers": "Everyone who made a reservation (incl. unpaid / cancelled)",
}

CSV_HEADER = ["Email", "Phone", "First Name", "Last Name", "Country", "Zip"]
_CONFIRMED_STATUS = {"captured", "confirmed", "fulfilled"}
_CANCELLED = {"cancelled"}
_TEST_DOMAINS = ("example.com", "example.org", "example.net", "test.com", "mailinator.com")


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    local, _, domain = e.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")   # Google's own normalisation
    return f"{local}@{domain}" if local and "." in domain else ""


def normalize_phone(phone: str, default_cc: str = "1") -> str:
    """E.164 (+15551234567). 10-digit numbers are assumed to be US/Canada."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits if 8 <= len(digits) <= 15 else ""
    if len(digits) == 10:
        return f"+{default_cc}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _is_test(r: Reservation) -> bool:
    email = (r.customer_email or "").lower()
    return (email.endswith(_TEST_DOMAINS) or "@test." in email
            or (r.customer_name or "").strip().upper().startswith("TEST"))


def _confirmed(r: Reservation) -> bool:
    return r.status in _CONFIRMED_STATUS or r.payment_status == "captured"


def _cancelled(r: Reservation) -> bool:
    return r.status in _CANCELLED or r.payment_status in ("refunded", "voided")


def normalize_name(name: str) -> str:
    """Google: lowercase, no punctuation or accents' marks kept as letters, no spaces at ends."""
    n = unicodedata.normalize("NFKC", (name or "")).strip().lower()
    n = re.sub(r"[^\w\s'-]", "", n)          # drop punctuation except hyphen/apostrophe
    n = re.sub(r"[\d_]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in normalize_name(full).split(" ") if p]
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[-1]


def normalize_zip(zip_: str, country: str) -> str:
    z = (zip_ or "").strip().upper()
    if country == "US":
        d = re.sub(r"\D", "", z)
        return d[:5] if len(d) >= 5 else ""
    return re.sub(r"\s+", " ", z)[:10]


def _value(r: Reservation) -> float:
    """Total booking value: rental price + what we charged online."""
    paid = r.amount_captured or r.pay_now_total
    return round((r.base_price or 0) + (paid or 0), 2)


def _when(r: Reservation) -> datetime | None:
    d = r.created_at
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


async def collect_people(session, segment: str) -> list[dict]:
    """People in a segment, de-duplicated by email (or phone), with every identifier."""
    if segment not in SEGMENTS:
        raise KeyError(segment)
    rows = (await session.execute(select(Reservation))).scalars().all()
    people: dict[str, dict] = {}
    for r in rows:
        if r.ads_opt_out or _is_test(r):
            continue
        email = normalize_email(r.customer_email)
        phone = normalize_phone(r.customer_phone)
        key = email or phone
        if not key:
            continue
        first, last = split_name(r.customer_name)
        country = (r.billing_country or "").strip().upper()
        zip_ = normalize_zip(r.billing_zip, country)
        p = people.setdefault(key, {"email": email, "phone": phone, "first": first,
                                    "last": last, "country": country, "zip": zip_,
                                    "confirmed": 0, "cancelled": 0, "any": 0,
                                    "insured": 0, "value": 0.0, "last_confirmed": None})
        p["phone"] = p["phone"] or phone
        if not (p["first"] and p["last"]):
            p["first"], p["last"] = first, last
        if not (p["country"] and p["zip"]) and country and zip_:
            p["country"], p["zip"] = country, zip_
        p["any"] += 1
        if _confirmed(r) and not _cancelled(r):
            p["confirmed"] += 1
            p["value"] += _value(r)
            when = _when(r)
            if when and (p["last_confirmed"] is None or when > p["last_confirmed"]):
                p["last_confirmed"] = when
            if (r.insurance_price or 0) > 0:
                p["insured"] += 1
        if _cancelled(r):
            p["cancelled"] += 1

    everyone = list(people.values())
    now = datetime.now(timezone.utc)

    if segment == "high-value":
        buyers = sorted((p for p in everyone if p["confirmed"] >= 1),
                        key=lambda p: p["value"], reverse=True)
        floor = float(os.environ.get("HIGH_VALUE_MIN_USD", "0") or 0)
        top_n = max(1, -(-len(buyers) // 5)) if buyers else 0      # top 20%, at least 1
        return [p for i, p in enumerate(buyers) if i < top_n or (floor and p["value"] >= floor)]

    def keep(p: dict) -> bool:
        lc = p["last_confirmed"]
        return {
            "customers": p["confirmed"] >= 1,
            "all-bookers": p["any"] >= 1,
            "cancelled": p["cancelled"] >= 1 and p["confirmed"] == 0,
            "repeat-customers": p["confirmed"] >= 2,
            "insurance-buyers": p["insured"] >= 1,
            "recent-customers": lc is not None and lc >= now - timedelta(days=90),
            "lapsed-customers": lc is not None and lc < now - timedelta(days=180),
            "unconverted": p["confirmed"] == 0 and p["cancelled"] == 0,
        }[segment]

    return [p for p in everyone if keep(p)]


async def collect(session, segment: str) -> list[tuple[str, str]]:
    """[(email_norm, phone_norm)] for a segment (kept for callers that only need ids)."""
    return [(p["email"], p["phone"]) for p in await collect_people(session, segment)]


async def audience_csv(session, segment: str) -> tuple[str, int]:
    """(csv_text, row_count): Google Customer Match hashed file, all identifiers."""
    people = await collect_people(session, segment)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for p in people:
        # Google only matches on address when name + country + zip are all present.
        full_addr = p["first"] and p["last"] and p["country"] and p["zip"]
        w.writerow([_sha(p["email"]), _sha(p["phone"]), _sha(p["first"]), _sha(p["last"]),
                    p["country"] if full_addr else "", p["zip"] if full_addr else ""])
    return buf.getvalue(), len(people)
