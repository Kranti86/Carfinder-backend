"""Small management commands.

  python -m app.cli create-agent <email> <password> [name] [role]
  python -m app.cli capture-due            # capture holds older than 3 days
  python -m app.cli daily-digest           # post the daily summary to Slack
  python -m app.cli daily                  # capture-due, then daily-digest (cron)
  python -m app.cli purge-reservations     # DELETE all reservations + audit logs
  python -m app.cli init-db                # create tables
  python -m app.cli export-conversions [conversion_name]
                                           # Google Ads Offline Conversion Import CSVs

Run capture-due daily via Heroku Scheduler.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from .auth import hash_password
from .db import init_db, session_factory
from .models_db import Agent, AuditLog, Reservation
from .services import conversions, notify, payments

CAPTURE_AFTER_DAYS = 3

# Google Ads offline import wants a name that matches a conversion action you create
# in the account (Goals → Conversions → New → Import → manual/CSV). Default below.
DEFAULT_CONVERSION_NAME = conversions.DEFAULT_CONVERSION_NAME


async def _create_agent(email, password, name="", role="agent"):
    await init_db()
    async with session_factory()() as s:
        exists = (await s.execute(select(Agent).where(Agent.email == email.lower()))).scalar_one_or_none()
        if exists:
            print(f"agent {email} already exists")
            return
        s.add(Agent(email=email.strip().lower(), name=name,
                    password_hash=hash_password(password), role=role))
        await s.commit()
        print(f"created {role} {email}")


async def _capture_due():
    await init_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=CAPTURE_AFTER_DAYS)
    async with session_factory()() as s:
        rows = (await s.execute(select(Reservation).where(
            Reservation.payment_status == "authorized",
            Reservation.status != "cancelled",   # never charge a cancelled booking
            Reservation.authorized_at.is_not(None),
            Reservation.authorized_at <= cutoff))).scalars().all()
        print(f"{len(rows)} reservation(s) due for capture")
        captured, failed = [], []
        for r in rows:
            res = await payments.capture(r.payment_ref, r.amount_authorized or r.pay_now_total)
            if res.ok:
                r.payment_status = "captured"
                r.amount_captured = r.amount_authorized or r.pay_now_total
                r.capture_ref = res.ref
                r.status = "captured"
                s.add(AuditLog(reservation_id=r.id, agent_email="scheduler",
                               action="auto_capture",
                               detail=f"${r.amount_captured:.2f} after {CAPTURE_AFTER_DAYS}d"))
                print(f"  captured {r.id[:8]} ${r.amount_captured:.2f}")
                captured.append(r)
            else:
                failed.append(r)
                s.add(AuditLog(reservation_id=r.id, agent_email="scheduler",
                               action="auto_capture_failed", detail=res.message))
        await s.commit()
    await notify.capture_run(captured, failed)
    return captured, failed


async def _daily_digest():
    await init_db()
    async with session_factory()() as s:
        text = await notify.send_digest(s)
    print(text if notify.enabled() else "SLACK_WEBHOOK_URL not set — digest not sent:\n" + text)


async def _export_conversions(conversion_name: str = DEFAULT_CONVERSION_NAME):
    """Emit two Google Ads Offline Conversion Import files, keyed on the stored gclid:

      ads_conversions.csv           — REAL, captured bookings, valued at actual margin.
      ads_conversion_retractions.csv — captured bookings later refunded/voided (RETRACTION).

    Same content the portal's one-click download and the auto-pull URL serve. Upload both
    in Google Ads → Goals → Conversions → Uploads (or let a scheduled upload fetch them).
    """
    await init_db()
    async with session_factory()() as s:
        conv_text, n_won = await conversions.conversions_csv(s, conversion_name)
        retr_text, n_retr = await conversions.retractions_csv(s, conversion_name)

    with open("ads_conversions.csv", "w", newline="") as f:
        f.write(conv_text)
    with open("ads_conversion_retractions.csv", "w", newline="") as f:
        f.write(retr_text)

    print(f"ads_conversions.csv           → {n_won} captured booking(s)")
    print(f"ads_conversion_retractions.csv → {n_retr} retraction(s)")
    print(f"Conversion action name used: {conversion_name!r} "
          "(must match the Import action you create in Google Ads).")


async def _purge_reservations():
    await init_db()
    async with session_factory()() as s:
        await s.execute(delete(AuditLog))
        res = await s.execute(delete(Reservation))
        await s.commit()
        print(f"deleted {res.rowcount if res.rowcount is not None else 'all'} reservations "
              f"(+ their audit logs). Agents and settings kept.")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "purge-reservations":
        asyncio.run(_purge_reservations())
        return
    if cmd == "export-conversions":
        asyncio.run(_export_conversions(args[1] if len(args) > 1 else DEFAULT_CONVERSION_NAME))
        return
    if cmd == "create-agent" and len(args) >= 3:
        asyncio.run(_create_agent(args[1], args[2],
                                  args[3] if len(args) > 3 else "",
                                  args[4] if len(args) > 4 else "agent"))
    elif cmd == "capture-due":
        asyncio.run(_capture_due())
    elif cmd == "daily-digest":
        asyncio.run(_daily_digest())
    elif cmd == "daily":
        async def both():
            await _capture_due()
            await _daily_digest()
        asyncio.run(both())
    elif cmd == "init-db":
        asyncio.run(init_db())
        print("tables ready")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
