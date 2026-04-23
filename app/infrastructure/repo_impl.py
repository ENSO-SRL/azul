"""
Concrete repository implementations — SQLAlchemy async.

Converts between domain entities and ORM models.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import (
    Payment,
    PaymentStatus,
    PaymentType,
    RecurringPayment,
    SubscriptionStatus,
    Transaction,
)
from app.domain.repositories import (
    PaymentRepository,
    RecurringRepository,
    TransactionRepository,
)
from app.infrastructure.models import (
    PaymentModel,
    RecurringPaymentModel,
    TransactionModel,
)


# ---------------------------------------------------------------------------
# Mappers  (entity ↔ ORM model)
# ---------------------------------------------------------------------------

def _payment_to_model(p: Payment) -> PaymentModel:
    return PaymentModel(
        id=p.id,
        order_id=p.order_id,
        amount=p.amount,
        itbis=p.itbis,
        currency=p.currency,
        card_number_masked=p.card_number_masked,
        payment_type=p.payment_type.value,
        status=p.status.value,
        auth_mode=p.auth_mode,
        initiated_by=p.initiated_by,
        idempotency_key=p.idempotency_key,
        azul_order_id=p.azul_order_id,
        iso_code=p.iso_code,
        response_code=p.response_code,
        response_message=p.response_message,
        data_vault_token=p.data_vault_token,
        service_type=p.service_type,
        bill_reference=p.bill_reference,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _model_to_payment(m: PaymentModel) -> Payment:
    return Payment(
        id=m.id,
        order_id=m.order_id,
        amount=m.amount,
        itbis=m.itbis,
        currency=m.currency,
        card_number_masked=m.card_number_masked,
        payment_type=PaymentType(m.payment_type),
        status=PaymentStatus(m.status),
        auth_mode=m.auth_mode,
        initiated_by=m.initiated_by,  # type: ignore[arg-type]
        idempotency_key=m.idempotency_key,
        azul_order_id=m.azul_order_id,
        iso_code=m.iso_code,
        response_code=m.response_code,
        response_message=m.response_message,
        data_vault_token=m.data_vault_token,
        service_type=m.service_type,
        bill_reference=m.bill_reference,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _recurring_to_model(r: RecurringPayment) -> RecurringPaymentModel:
    return RecurringPaymentModel(
        id=r.id,
        customer_id=r.customer_id,
        amount=r.amount,
        itbis=r.itbis,
        frequency_days=r.frequency_days,
        description=r.description,
        status=r.status.value,
        data_vault_token=r.data_vault_token,
        card_brand=r.card_brand,
        card_last4=r.card_last4,
        next_charge_at=r.next_charge_at,
        last_charged_at=r.last_charged_at,
        created_at=r.created_at,
    )


def _model_to_recurring(m: RecurringPaymentModel) -> RecurringPayment:
    return RecurringPayment(
        id=m.id,
        customer_id=m.customer_id,
        amount=m.amount,
        itbis=m.itbis,
        frequency_days=m.frequency_days,
        description=m.description,
        status=SubscriptionStatus(m.status),
        data_vault_token=m.data_vault_token,
        card_brand=m.card_brand,
        card_last4=m.card_last4,
        next_charge_at=m.next_charge_at,
        last_charged_at=m.last_charged_at,
        created_at=m.created_at,
    )


def _txn_to_model(t: Transaction) -> TransactionModel:
    return TransactionModel(
        id=t.id,
        payment_id=t.payment_id,
        request_payload=t.request_payload,
        response_payload=t.response_payload,
        http_status=t.http_status,
        iso_code=t.iso_code,
        response_code=t.response_code,
        response_message=t.response_message,
        created_at=t.created_at,
    )


def _model_to_txn(m: TransactionModel) -> Transaction:
    return Transaction(
        id=m.id,
        payment_id=m.payment_id,
        request_payload=m.request_payload,
        response_payload=m.response_payload,
        http_status=m.http_status,
        iso_code=m.iso_code,
        response_code=getattr(m, "response_code", ""),
        response_message=m.response_message,
        created_at=m.created_at,
    )


# ---------------------------------------------------------------------------
# Concrete repos
# ---------------------------------------------------------------------------

class SQLPaymentRepository(PaymentRepository):

    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, payment: Payment) -> Payment:
        self._session.add(_payment_to_model(payment))
        await self._session.commit()
        return payment

    async def get_by_id(self, payment_id: str) -> Payment | None:
        result = await self._session.execute(
            select(PaymentModel).where(PaymentModel.id == payment_id)
        )
        row = result.scalar_one_or_none()
        return _model_to_payment(row) if row else None

    async def find_by_idempotency_key(self, key: str) -> Payment | None:
        if not key:
            return None
        result = await self._session.execute(
            select(PaymentModel).where(PaymentModel.idempotency_key == key)
        )
        row = result.scalar_one_or_none()
        return _model_to_payment(row) if row else None

    async def update(self, payment: Payment) -> Payment:
        result = await self._session.execute(
            select(PaymentModel).where(PaymentModel.id == payment.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.order_id         = payment.order_id
            model.amount           = payment.amount
            model.itbis            = payment.itbis
            model.status           = payment.status.value
            model.iso_code         = payment.iso_code
            model.response_code    = payment.response_code
            model.azul_order_id    = payment.azul_order_id
            model.response_message = payment.response_message
            model.card_number_masked = payment.card_number_masked
            model.data_vault_token = payment.data_vault_token
            model.service_type     = payment.service_type
            model.bill_reference   = payment.bill_reference
            model.updated_at       = datetime.now(timezone.utc)
            await self._session.commit()
        return payment


class SQLRecurringRepository(RecurringRepository):

    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, recurring: RecurringPayment) -> RecurringPayment:
        self._session.add(_recurring_to_model(recurring))
        await self._session.commit()
        return recurring

    async def get_by_id(self, recurring_id: str) -> RecurringPayment | None:
        result = await self._session.execute(
            select(RecurringPaymentModel).where(RecurringPaymentModel.id == recurring_id)
        )
        row = result.scalar_one_or_none()
        return _model_to_recurring(row) if row else None

    async def update(self, recurring: RecurringPayment) -> RecurringPayment:
        result = await self._session.execute(
            select(RecurringPaymentModel).where(RecurringPaymentModel.id == recurring.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.amount           = recurring.amount
            model.itbis            = recurring.itbis
            model.frequency_days   = recurring.frequency_days
            model.description      = recurring.description
            model.data_vault_token = recurring.data_vault_token
            model.card_brand       = recurring.card_brand
            model.card_last4       = recurring.card_last4
            model.next_charge_at   = recurring.next_charge_at
            model.last_charged_at  = recurring.last_charged_at
            model.status           = recurring.status.value
            await self._session.commit()
        return recurring

    async def list_active(self) -> list[RecurringPayment]:
        result = await self._session.execute(
            select(RecurringPaymentModel).where(
                RecurringPaymentModel.status == SubscriptionStatus.ACTIVE.value
            )
        )
        return [_model_to_recurring(r) for r in result.scalars().all()]

    async def list_due(self) -> list[RecurringPayment]:
        """Return active subscriptions whose next_charge_at <= now (UTC)."""
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            select(RecurringPaymentModel).where(
                RecurringPaymentModel.status == SubscriptionStatus.ACTIVE.value,
                RecurringPaymentModel.next_charge_at <= now,
            )
        )
        return [_model_to_recurring(r) for r in result.scalars().all()]


class SQLTransactionRepository(TransactionRepository):

    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, txn: Transaction) -> Transaction:
        self._session.add(_txn_to_model(txn))
        await self._session.commit()
        return txn

    async def list_by_payment(self, payment_id: str) -> list[Transaction]:
        result = await self._session.execute(
            select(TransactionModel).where(
                TransactionModel.payment_id == payment_id
            ).order_by(TransactionModel.created_at.desc())
        )
        return [_model_to_txn(r) for r in result.scalars().all()]
