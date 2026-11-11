"""Google Ads Customer Match lists built from reservations.

Each segment is a CSV in Google's "hashed data file" format: the headers Google
expects ("Email", "Phone"), values normalised the way Google specifies, then
SHA-256 hashed (hex), so no raw email or phone ever leaves the server.

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
import re

from sqlalchemy import select

from ..models_db import Reservation

SEGMENTS: dict[str, str] = {
    "customers": "Confirmed customers: paid, confirmed or completed rentals",
    "all-bookers": "Everyone who made a reservation (incl. unpaid / cancelled)",
    "cancelled": "Reservations that were cancelled, refunded or voided",
    "repeat-customers": "Customers with 2 or more confirmed rentals",
    "insurance-buyers": "Confirmed customers who bought insurance",
}

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


async def collect(session, segment: str) -> list[tuple[str, str]]:
    """[(email_norm, phone_norm)] for a segment, de-duplicated by person."""
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
        p = people.setdefault(key, {"email": email, "phone": phone, "confirmed": 0,
                                    "cancelled": 0, "any": 0, "insured": 0})
        p["phone"] = p["phone"] or phone
        p["any"] += 1
        if _confirmed(r) and not _cancelled(r):
            p["confirmed"] += 1
            if (r.insurance_price or 0) > 0:
                p["insured"] += 1
        if _cancelled(r):
            p["cancelled"] += 1

    def keep(p: dict) -> bool:
        return {
            "customers": p["confirmed"] >= 1,
            "all-bookers": p["any"] >= 1,
            "cancelled": p["cancelled"] >= 1 and p["confirmed"] == 0,
            "repeat-customers": p["confirmed"] >= 2,
            "insurance-buyers": p["insured"] >= 1,
        }[segment]

    return [(p["email"], p["phone"]) for p in people.values() if keep(p)]


async def audience_csv(session, segment: str) -> tuple[str, int]:
    """(csv_text, row_count): Google Customer Match hashed file (Email, Phone)."""
    people = await collect(session, segment)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Email", "Phone"])
    for email, phone in people:
        w.writerow([_sha(email), _sha(phone)])
    return buf.getvalue(), len(people)
