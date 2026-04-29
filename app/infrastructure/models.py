"""
SQLAlchemy ORM models — mirrors domain entities for persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
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

    # CIT vs MIT
    initiated_by: Mapped[str] = mapped_column(String(20), default="cardholder")

    # Idempotencia — UNIQUE permite buscar por clave y evitar cobros dobles
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)

    # Azul response fields
    azul_order_id: Mapped[str] = mapped_column(String(50), default="")
    iso_code: Mapped[str] = mapped_column(String(10), default="")
    response_code: Mapped[str] = mapped_column(String(20), default="")
    response_message: Mapped[str] = mapped_column(String(255), default="")

    # DataVault token (si se solicitó save_card=True)
    data_vault_token: Mapped[str] = mapped_column(String(100), default="")

    # Campos obligatorios Azul API v1.2
    cardholder_name: Mapped[str] = mapped_column(String(100), default="")
    cardholder_email: Mapped[str] = mapped_column(String(255), default="")

    # Service payment fields
    service_type: Mapped[str] = mapped_column(String(50), default="")
    bill_reference: Mapped[str] = mapped_column(String(100), default="")

    # 3DS 2.0
    threeds_method_form: Mapped[str] = mapped_column(Text, default="")
    threeds_redirect_url: Mapped[str] = mapped_column(String(500), default="")
    threeds_challenge_form: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class SavedCardModel(Base):
    __tablename__ = "saved_cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    card_brand: Mapped[str] = mapped_column(String(20), default="")
    card_last4: Mapped[str] = mapped_column(String(4), default="")
    expiration: Mapped[str] = mapped_column(String(6), default="")   # YYYYMM
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
    card_expiration: Mapped[str] = mapped_column(String(6), default="")  # YYYYMM

    # Scheduling
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Retry policy — conteo explícito de intentos fallidos consecutivos
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_reason: Mapped[str] = mapped_column(String(500), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ConsentModel(Base):
    """Audit trail — one row per subscription enrolment consent.

    Stores the exact text shown to the customer, their IP, and the
    timestamp so we have documented evidence for Visa/MC disputes.
    """

    __tablename__ = "consent_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    consent_text: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), default="")   # IPv6 max length
    user_agent: Mapped[str] = mapped_column(String(500), default="")

    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TransactionModel(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_payload: Mapped[str] = mapped_column(Text, default="")   # PAN enmascarado
    response_payload: Mapped[str] = mapped_column(Text, default="")
    http_status: Mapped[int] = mapped_column(Integer, default=0)
    iso_code: Mapped[str] = mapped_column(String(10), default="")
    response_code: Mapped[str] = mapped_column(String(20), default="")
    response_message: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReconciliationReportModel(Base):
    """Daily reconciliation result — one row per payment checked.

    Stores the comparison between what Atlas recorded locally and what
    Azul reports via verify_payment. Rows with status='MISMATCH' need
    manual review.
    """

    __tablename__ = "reconciliation_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    payment_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    custom_order_id: Mapped[str] = mapped_column(String(128), default="")
    local_status: Mapped[str] = mapped_column(String(20), default="")     # Atlas PaymentStatus
    local_iso_code: Mapped[str] = mapped_column(String(10), default="")
    azul_status: Mapped[str] = mapped_column(String(20), default="")      # From verify_payment
    azul_iso_code: Mapped[str] = mapped_column(String(10), default="")
    azul_order_id: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[str] = mapped_column(String(20), default="OK")         # OK | MISMATCH | NOT_FOUND | ERROR
    notes: Mapped[str] = mapped_column(String(500), default="")

    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

