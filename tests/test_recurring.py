"""
Tests for the recurring payment service.

Uses unittest.mock to avoid real Azul API calls.
Run with: pytest tests/test_recurring.py -v
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.entities import (
    ConsentRecord,
    IsoCode,
    Payment,
    PaymentStatus,
    PaymentType,
    RecurringPayment,
    SubscriptionStatus,
    Transaction,
)
from app.services.recurring_service import RecurringService


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_approved_payment(token: str = "TEST-TOKEN-UUID") -> Payment:
    p = Payment(amount=5000, itbis=900, payment_type=PaymentType.RECURRING)
    p.status = PaymentStatus.APPROVED
    p.iso_code = IsoCode.APPROVED
    p.data_vault_token = token
    p.card_number_masked = "4260********5872"
    return p


def _make_txn(payment_id: str = "p1") -> Transaction:
    return Transaction(payment_id=payment_id, http_status=200, iso_code="00")


def _make_recurring(
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    token: str = "TEST-TOKEN-UUID",
    card_expiration: str = "203012",
) -> RecurringPayment:
    now = datetime.now(timezone.utc)
    return RecurringPayment(
        id="sub-test-id",
        customer_id="CLI-001",
        amount=5000,
        itbis=900,
        data_vault_token=token,
        card_expiration=card_expiration,
        status=status,
        next_charge_at=now - timedelta(minutes=5),  # overdue
        last_charged_at=now - timedelta(days=30),
    )


def _make_service(
    *,
    gateway_sale_recurring_cit=None,
    gateway_sale_mit=None,
    gateway_delete_token=None,
    saved_recurring=None,
) -> RecurringService:
    """Build a RecurringService with all repos mocked."""
    payment_repo   = AsyncMock()
    recurring_repo = AsyncMock()
    txn_repo       = AsyncMock()
    consent_repo   = AsyncMock()
    gateway        = AsyncMock()

    payment_repo.save.return_value = None
    txn_repo.save.return_value = None
    recurring_repo.save.return_value = None
    recurring_repo.update.return_value = None

    if gateway_sale_recurring_cit:
        gateway.sale_recurring_cit = gateway_sale_recurring_cit
    if gateway_sale_mit:
        gateway.sale_mit = gateway_sale_mit
    if gateway_delete_token:
        gateway.delete_token = gateway_delete_token

    if saved_recurring is not None:
        recurring_repo.get_by_id.return_value = saved_recurring

    return RecurringService(
        payment_repo=payment_repo,
        recurring_repo=recurring_repo,
        txn_repo=txn_repo,
        gateway=gateway,
        consent_repo=consent_repo,
    )


# ---------------------------------------------------------------------------
# create_subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_subscription_happy_path():
    """First charge should use STANDING_ORDER CIT and persist the subscription."""
    payment = _make_approved_payment("TOKEN-ABC")
    txn     = _make_txn(payment.id)

    cit = AsyncMock(return_value=(payment, txn))
    svc = _make_service(gateway_sale_recurring_cit=cit)

    recurring, initial_payment = await svc.create_subscription(
        customer_id="CLI-001",
        amount=5000,
        itbis=900,
        card_number="4260550061845872",
        expiration="203012",
        cvc="123",
        cardholder_name="Juan Pérez",
        cardholder_email="juan@ejemplo.com",
    )

    # Gateway should have been called with STANDING_ORDER CIT method
    cit.assert_awaited_once()

    # Subscription should have the token and expiration
    assert recurring.data_vault_token == "TOKEN-ABC"
    assert recurring.card_expiration  == "203012"
    assert recurring.card_last4       == "5872"
    assert recurring.status           == SubscriptionStatus.ACTIVE
    assert recurring.next_charge_at is not None

    # Repos should have persisted both the payment and the subscription
    svc._payments.save.assert_awaited_once()
    svc._recurring.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_subscription_declined_does_not_save():
    """A declined first charge should NOT persist a subscription."""
    payment = Payment(amount=5000, itbis=900, payment_type=PaymentType.RECURRING)
    payment.status = PaymentStatus.DECLINED
    payment.iso_code = IsoCode.DECLINED_FUNDS
    txn = _make_txn(payment.id)

    cit = AsyncMock(return_value=(payment, txn))
    svc = _make_service(gateway_sale_recurring_cit=cit)

    recurring, initial_payment = await svc.create_subscription(
        customer_id="CLI-001",
        amount=5000,
        itbis=900,
        card_number="4260550061845872",
        expiration="203012",
        cvc="123",
        cardholder_name="Test",
        cardholder_email="t@t.com",
    )

    # No subscription should be saved on decline
    svc._recurring.save.assert_not_awaited()
    assert initial_payment.status == PaymentStatus.DECLINED


# ---------------------------------------------------------------------------
# charge (MIT)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_charge_mit_happy_path():
    """MIT charge should succeed and advance next_charge_at."""
    sub = _make_recurring()
    payment = _make_approved_payment()
    txn = _make_txn(payment.id)

    mit = AsyncMock(return_value=(payment, txn))
    svc = _make_service(gateway_sale_mit=mit, saved_recurring=sub)

    result = await svc.charge("sub-test-id")

    mit.assert_awaited_once()
    assert result.status == PaymentStatus.APPROVED
    svc._recurring.update.assert_awaited_once()


@pytest.mark.asyncio
async def test_charge_raises_if_no_token():
    """Charge should raise ValueError if subscription has no DataVault token."""
    sub = _make_recurring(token="")
    svc = _make_service(saved_recurring=sub)

    with pytest.raises(ValueError, match="no DataVault token"):
        await svc.charge("sub-test-id")


@pytest.mark.asyncio
async def test_charge_raises_if_paused():
    """Charge should raise if subscription is PAUSED."""
    sub = _make_recurring(status=SubscriptionStatus.PAUSED)
    svc = _make_service(saved_recurring=sub)

    with pytest.raises(ValueError, match="PAUSED"):
        await svc.charge("sub-test-id")


# ---------------------------------------------------------------------------
# cancel_subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_calls_datavault_delete():
    """Cancellation must call DataVault DELETE on the stored token."""
    sub = _make_recurring()
    delete_token = AsyncMock()
    svc = _make_service(gateway_delete_token=delete_token, saved_recurring=sub)

    result = await svc.cancel_subscription("sub-test-id")

    delete_token.assert_awaited_once_with("TEST-TOKEN-UUID")
    assert result.status == SubscriptionStatus.CANCELLED
    assert result.data_vault_token == ""  # cleared locally


@pytest.mark.asyncio
async def test_cancel_succeeds_even_if_delete_fails():
    """DataVault DELETE failure must NOT block subscription cancellation."""
    sub = _make_recurring()
    delete_token = AsyncMock(side_effect=Exception("Network error"))
    svc = _make_service(gateway_delete_token=delete_token, saved_recurring=sub)

    # Should not raise
    result = await svc.cancel_subscription("sub-test-id")
    assert result.status == SubscriptionStatus.CANCELLED


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_subscription():
    """pause_subscription should delegate to the repo."""
    sub = _make_recurring()
    svc = _make_service(saved_recurring=sub)
    paused = _make_recurring(status=SubscriptionStatus.PAUSED)
    svc._recurring.pause.return_value = paused

    result = await svc.pause_subscription("sub-test-id")

    svc._recurring.pause.assert_awaited_once_with("sub-test-id")
    assert result.status == SubscriptionStatus.PAUSED


@pytest.mark.asyncio
async def test_resume_subscription():
    """resume_subscription should reset failed_attempts and go ACTIVE."""
    sub = _make_recurring(status=SubscriptionStatus.PAUSED)
    svc = _make_service(saved_recurring=sub)
    resumed = _make_recurring(status=SubscriptionStatus.ACTIVE)
    resumed.failed_attempts = 0
    svc._recurring.resume.return_value = resumed

    result = await svc.resume_subscription("sub-test-id")

    svc._recurring.resume.assert_awaited_once_with("sub-test-id")
    assert result.status == SubscriptionStatus.ACTIVE
    assert result.failed_attempts == 0


# ---------------------------------------------------------------------------
# record_consent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_consent_persists_evidence():
    """Consent record should be saved with all Visa/MC required fields."""
    sub = _make_recurring()
    svc = _make_service(saved_recurring=sub)

    consent = ConsentRecord(
        subscription_id="sub-test-id",
        customer_id="CLI-001",
        consent_text="Acepto que se me cobre RD$500 cada 30 días.",
        ip_address="10.0.0.1",
        user_agent="Mozilla/5.0",
    )
    svc._consents.save.return_value = consent

    result = await svc.record_consent(
        subscription_id="sub-test-id",
        customer_id="CLI-001",
        consent_text="Acepto que se me cobre RD$500 cada 30 días.",
        ip_address="10.0.0.1",
        user_agent="Mozilla/5.0",
    )

    svc._consents.save.assert_awaited_once()
    assert result.subscription_id == "sub-test-id"
    assert result.ip_address == "10.0.0.1"
    assert "30 días" in result.consent_text
