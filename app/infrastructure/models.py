"""
SQLAlchemy ORM models — mirrors domain entities for persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentModel(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(100), default="")
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    itbis: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(5), default="$")
    card_number_masked: Mapped[str] = mapped_column(String(20), default="")
    payment_type: Mapped[str] = mapped_column(String(20), default="SALE")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    auth_mode: Mapped[str] = mapped_column(String(20), default="splitit")

    # Azul response fields
    azul_order_id: Mapped[str] = mapped_column(String(50), default="")
    iso_code: Mapped[str] = mapped_column(String(10), default="")
    response_message: Mapped[str] = mapped_column(String(255), default="")

    # Service payment fields
    service_type: Mapped[str] = mapped_column(String(50), default="")
    bill_reference: Mapped[str] = mapped_column(String(100), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class RecurringPaymentModel(Base):
    __tablename__ = "recurring_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    itbis: Mapped[int] = mapped_column(Integer, default=0)
    frequency_days: Mapped[int] = mapped_column(Integer, default=30)
    description: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")

    # DataVault token
    data_vault_token: Mapped[str] = mapped_column(String(100), default="")
    card_brand: Mapped[str] = mapped_column(String(20), default="")
    card_last4: Mapped[str] = mapped_column(String(4), default="")

    # Scheduling
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TransactionModel(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_payload: Mapped[str] = mapped_column(Text, default="")
    response_payload: Mapped[str] = mapped_column(Text, default="")
    http_status: Mapped[int] = mapped_column(Integer, default=0)
    iso_code: Mapped[str] = mapped_column(String(10), default="")
    response_message: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
