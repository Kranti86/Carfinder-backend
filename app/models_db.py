"""ORM models for Phase 2: agents, reservations, and an audit trail.

The Reservation is the "deal funnel" row the portal shows. Money is tracked in two
buckets: what we hold/collect online (platform service fee + insurance) vs. the rental
the customer pays the supplier at the counter (never charged by us).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary,
                        String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Deal-funnel + payment status vocabularies (kept as plain strings for clarity)
# --------------------------------------------------------------------------- #
STATUS_FLOW = ["new", "authorized", "confirmed", "captured", "fulfilled", "cancelled"]
PAYMENT_STATES = ["none", "authorized", "captured", "refunded", "voided", "failed"]


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="agent")  # agent | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    source: Mapped[str] = mapped_column(String(20), default="online")  # online | phone

    # customer
    customer_name: Mapped[str] = mapped_column(String(160), default="")
    customer_email: Mapped[str] = mapped_column(String(255), default="", index=True)
    customer_phone: Mapped[str] = mapped_column(String(40), default="")

    # trip
    city: Mapped[str] = mapped_column(String(120), default="")
    pickup_area: Mapped[str] = mapped_column(String(200), default="")
    pickup_datetime: Mapped[str] = mapped_column(String(40), default="")
    dropoff_datetime: Mapped[str] = mapped_column(String(40), default="")

    # vehicle / supplier
    vehicle_name: Mapped[str] = mapped_column(String(160), default="")
    vehicle_type: Mapped[str] = mapped_column(String(60), default="")
    supplier: Mapped[str] = mapped_column(String(120), default="")

    # money (USD). base_price = rental paid at counter (we never charge it).
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    base_price: Mapped[float] = mapped_column(Float, default=0.0)
    service_fee: Mapped[float] = mapped_column(Float, default=0.0)
    insurance_price: Mapped[float] = mapped_column(Float, default=0.0)
    insurance_net: Mapped[float] = mapped_column(Float, default=0.0)
    amount_authorized: Mapped[float] = mapped_column(Float, default=0.0)  # service_fee + insurance
    amount_captured: Mapped[float] = mapped_column(Float, default=0.0)

    # payment + fulfilment
    payment_status: Mapped[str] = mapped_column(String(20), default="none")
    paypal_order_id: Mapped[str] = mapped_column(String(120), default="", index=True)  # PayPal order (pre-hold)
    payment_ref: Mapped[str] = mapped_column(String(120), default="")   # PayPal authorization id (the hold)
    capture_ref: Mapped[str] = mapped_column(String(120), default="")   # PayPal capture id (for refunds)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policy_number: Mapped[str] = mapped_column(String(120), default="")  # insurance certificate
    supplier_reservation_number: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)

    # final supplier confirmation (agent attaches the receipt + sends it to the customer)
    confirmation_doc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    confirmation_doc_name: Mapped[str] = mapped_column(String(200), default="")
    confirmation_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmation_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # attribution
    gclid: Mapped[str] = mapped_column(String(255), default="")
    utm_campaign: Mapped[str] = mapped_column(String(255), default="")

    events: Mapped[list["AuditLog"]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan",
        order_by="AuditLog.created_at.desc()")

    @property
    def pay_now_total(self) -> float:
        return round(self.service_fee + self.insurance_price, 2)


class Setting(Base):
    """Simple key/value app settings (e.g. service_fee_enabled = 'true'/'false')."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    agent_email: Mapped[str] = mapped_column(String(255), default="system")
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    reservation: Mapped[Reservation] = relationship(back_populates="events")
