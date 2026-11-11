"""Build Google Ads Offline Conversion Import CSVs from reservations.

Shared by the CLI (`export-conversions`), the admin one-click download, and the
token-protected auto-pull URL that Google Ads scheduled uploads fetch on their own.

We only ever export bookings that carry a stored gclid (paid-click bookings):
  * "won"      → payment CAPTURED and not reversed → reported at actual MARGIN.
  * "retracted"→ a captured booking later refunded/voided → RETRACTION row.
Conversion Time is the reservation's created_at (always after the click and inside the
lookback window); the retraction reuses that same time so Google can match the row.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from sqlalchemy import select

from ..models_db import Reservation

DEFAULT_CONVERSION_NAME = "Confirmed booking"


def _margin(r: Reservation) -> float:
    """What we actually earn: platform fee + insurance commission. The rental (base)
    is paid to the supplier at the counter and the insurance net is pass-through."""
    ins_commission = max(0.0, (r.insurance_price or 0.0) - (r.insurance_net or 0.0))
    return round((r.service_fee or 0.0) + ins_commission, 2)


def _gads_time(dt: datetime | None) -> str:
    """Google Ads accepts 'yyyy-MM-dd HH:mm:ss+00:00'. We store UTC → emit +00:00."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


async def collect(session, conversion_name: str = DEFAULT_CONVERSION_NAME):
    """Return (won_rows, retracted_rows) as lists of CSV tuples."""
    rows = (await session.execute(
        select(Reservation).where(Reservation.gclid != ""))).scalars().all()
    won, retracted = [], []
    now = _gads_time(datetime.now(timezone.utc))
    for r in rows:
        if not r.gclid or r.ads_opt_out:
            continue   # visitor clicked "Reject" on the cookie banner: never report to Ads
        is_captured = (r.payment_status == "captured") or (r.status in ("captured", "fulfilled"))
        is_reversed = r.payment_status in ("refunded", "voided") or r.status == "cancelled"
        ctime = _gads_time(r.created_at)
        if is_captured and not is_reversed:
            won.append((r.gclid, conversion_name, ctime, f"{_margin(r):.2f}", r.currency or "USD"))
        elif is_reversed and (r.amount_captured or 0) > 0:
            # only retract what we previously reported (money had been captured)
            retracted.append((r.gclid, conversion_name, ctime, "RETRACTION", now))
    return won, retracted


def _render(param_line, columns, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([param_line])
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue()


async def conversions_csv(session, conversion_name: str = DEFAULT_CONVERSION_NAME):
    """(csv_text, row_count) for the captured-bookings upload."""
    won, _ = await collect(session, conversion_name)
    text = _render("Parameters:TimeZone=+0000",
                   ["Google Click ID", "Conversion Name", "Conversion Time",
                    "Conversion Value", "Conversion Currency"], won)
    return text, len(won)


async def retractions_csv(session, conversion_name: str = DEFAULT_CONVERSION_NAME):
    """(csv_text, row_count) for the refund/void retractions upload."""
    _, retracted = await collect(session, conversion_name)
    text = _render("Parameters:TimeZone=+0000",
                   ["Google Click ID", "Conversion Name", "Conversion Time",
                    "Adjustment Type", "Adjustment Time"], retracted)
    return text, len(retracted)
