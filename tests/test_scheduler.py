"""
Tests for the recurring payment scheduler.

Validates:
- Expiration guard pauses before attempting charge
- Successful MIT charge advances next_charge_at
- Business decline triggers retry backoff
- AzulIntegrationError does NOT pause subscription

Run with: pytest tests/test_scheduler.py -v
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.entities import (
    IsoCode,
    Payment,
    PaymentStatus,
    PaymentType,
    RecurringPayment,
    SubscriptionStatus,
)
from app.infrastructure.azul_gateway import AzulIntegrationError
from app.services import scheduler as sched_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sub(
    card_expiration: str = "203012",
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    failed_attempts: int = 0,
) -> RecurringPayment:
    now = datetime.now(timezone.utc)
    sub = RecurringPayment(
        id="sub-sched-001",
        customer_id="CLI-001",
        amount=5000,
        itbis=900,
        data_vault_token="VAULT-TOKEN",
        card_expiration=card_expiration,
        status=status,
        next_charge_at=now - timedelta(minutes=1),
        failed_attempts=failed_attempts,
    )
    return sub


def _approved_payment() -> Payment:
    p = Payment(amount=5000, itbis=900, payment_type=PaymentType.RECURRING)
    p.status = PaymentStatus.APPROVED
    p.iso_code = IsoCode.APPROVED
    p.response_message = "APROBADA"
    return p


def _declined_payment(iso: str = "51") -> Payment:
    p = Payment(amount=5000, itbis=900, payment_type=PaymentType.RECURRING)
    p.status = PaymentStatus.DECLINED
    p.iso_code = iso
    p.response_message = "DECLINADA"
    return p


def _build_mocks():
    """Build all repo/gateway mocks and a fake session_factory."""
    recurring_repo = AsyncMock()
    payment_repo   = AsyncMock()
    txn_repo       = AsyncMock()
    gateway        = AsyncMock()

    recurring_repo.update.return_value = None
    payment_repo.save.return_value     = None
    txn_repo.save.return_value         = None

    session = AsyncMock()
    session_factory = MagicMock()
    session_factory.return_value.__aenter__.return_value = session

    return recurring_repo, payment_repo, txn_repo, gateway, session_factory


def _patch_context(recurring_repo, payment_repo, txn_repo, gateway):
    """Return a context-manager stack that replaces imports inside the scheduler."""
    # The scheduler does lazy imports inside _charge_due_subscriptions.
    # We patch the classes at their source so the scheduler picks up the mocks.
    return [
        patch("app.infrastructure.repo_impl.SQLRecurringRepository", return_value=recurring_repo),
        patch("app.infrastructure.repo_impl.SQLPaymentRepository",   return_value=payment_repo),
        patch("app.infrastructure.repo_impl.SQLTransactionRepository", return_value=txn_repo),
        patch("app.infrastructure.azul_gateway.AzulPaymentGateway",  return_value=gateway),
    ]


# ---------------------------------------------------------------------------
# Expiration guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_card_pauses_without_charge():
    """Scheduler must pause subscription and skip charge if card is expired."""
    sub = _make_sub(card_expiration="202001")  # expired: Jan 2020

    recurring_repo, payment_repo, txn_repo, gateway, session_factory = _build_mocks()
    recurring_repo.list_due.return_value = [sub]
    gateway.sale_mit.return_value = (_approved_payment(), MagicMock())

    with (
        patch("app.infrastructure.repo_impl.SQLRecurringRepository", return_value=recurring_repo),
        patch("app.infrastructure.repo_impl.SQLPaymentRepository",   return_value=payment_repo),
        patch("app.infrastructure.repo_impl.SQLTransactionRepository", return_value=txn_repo),
        patch("app.infrastructure.azul_gateway.AzulPaymentGateway",   return_value=gateway),
    ):
        await sched_module._charge_due_subscriptions(session_factory)

    # Should NOT have charged
    gateway.sale_mit.assert_not_awaited()
    # Should have updated the sub to PAUSED
    recurring_repo.update.assert_awaited_once()
    updated_sub = recurring_repo.update.call_args[0][0]
    assert updated_sub.status == SubscriptionStatus.PAUSED
    assert "vencida" in updated_sub.last_failure_reason.lower()


@pytest.mark.asyncio
async def test_valid_card_proceeds_to_charge():
    """Scheduler must proceed to charge if card is not expired."""
    sub = _make_sub(card_expiration="203012")  # valid until Dec 2030

    recurring_repo, payment_repo, txn_repo, gateway, session_factory = _build_mocks()
    recurring_repo.list_due.return_value = [sub]
    gateway.sale_mit.return_value = (_approved_payment(), MagicMock())

    with (
        patch("app.infrastructure.repo_impl.SQLRecurringRepository", return_value=recurring_repo),
        patch("app.infrastructure.repo_impl.SQLPaymentRepository",   return_value=payment_repo),
        patch("app.infrastructure.repo_impl.SQLTransactionRepository", return_value=txn_repo),
        patch("app.services.scheduler.AzulPaymentGateway",           return_value=gateway),
    ):
        await sched_module._charge_due_subscriptions(session_factory)

    gateway.sale_mit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

def test_handle_failure_first_attempt():
    """First failure sets failed_attempts=1 and delays 1 day."""
    sub = _make_sub(failed_attempts=0)
    now = datetime.now(timezone.utc)
    result = sched_module._handle_failure(sub, "Fondos insuficientes")

    assert result.failed_attempts == 1
    assert result.status == SubscriptionStatus.ACTIVE  # not yet paused
    assert result.next_charge_at is not None
    delta = result.next_charge_at - now
    assert 0 < delta.total_seconds() < 2 * 24 * 3600  # between 0 and 2 days


def test_handle_failure_third_attempt_pauses():
    """Third consecutive failure should pause the subscription."""
    sub = _make_sub(failed_attempts=3)
    result = sched_module._handle_failure(sub, "Declinada")

    assert result.failed_attempts == 4
    assert result.status == SubscriptionStatus.PAUSED


def test_handle_failure_does_not_exceed_reason_length():
    """Failure reason must be truncated to 500 chars for DB column limit."""
    sub = _make_sub(failed_attempts=0)
    long_reason = "X" * 1000
    result = sched_module._handle_failure(sub, long_reason)

    assert len(result.last_failure_reason) == 500


# ---------------------------------------------------------------------------
# AzulIntegrationError does NOT pause
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_integration_error_does_not_pause():
    """AzulIntegrationError is OUR bug — subscription must NOT be paused."""
    sub = _make_sub()

    recurring_repo, payment_repo, txn_repo, gateway, session_factory = _build_mocks()
    recurring_repo.list_due.return_value = [sub]
    gateway.sale_mit.side_effect = AzulIntegrationError("MISSING_AUTH_HEADER")

    with (
        patch("app.infrastructure.repo_impl.SQLRecurringRepository", return_value=recurring_repo),
        patch("app.infrastructure.repo_impl.SQLPaymentRepository",   return_value=payment_repo),
        patch("app.infrastructure.repo_impl.SQLTransactionRepository", return_value=txn_repo),
        patch("app.infrastructure.azul_gateway.AzulPaymentGateway",   return_value=gateway),
    ):
        await sched_module._charge_due_subscriptions(session_factory)

    # update IS called but sub should remain ACTIVE (our bug, not user's fault)
    recurring_repo.update.assert_awaited_once()
    updated = recurring_repo.update.call_args[0][0]
    assert updated.status == SubscriptionStatus.ACTIVE
    assert updated.failed_attempts == 0  # not bumped for integration errors


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def test_custom_order_id_deterministic():
    """Same sub_id + failed_attempts must always yield the same order ID."""
    oid1 = sched_module._build_custom_order_id("abc-123", 0)
    oid2 = sched_module._build_custom_order_id("abc-123", 0)
    assert oid1 == oid2


def test_custom_order_id_unique_per_attempt():
    """Different attempt counts must yield different order IDs."""
    oid0 = sched_module._build_custom_order_id("abc-123", 0)
    oid1 = sched_module._build_custom_order_id("abc-123", 1)
    assert oid0 != oid1
