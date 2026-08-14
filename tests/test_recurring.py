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
    Currency,
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
    svc._payments.save.assert_awaited_once()   # type: ignore[attr-defined]
    svc._recurring.save.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_create_subscription_persists_currency_usd():
    """currency=USD must reach BOTH the first charge and the saved subscription.

    Regression: the currency field was accepted by the API but silently dropped,
    so a USD subscription was charged in DOP.
    """
    payment = _make_approved_payment("TOKEN-USD")
    txn     = _make_txn(payment.id)

    cit = AsyncMock(return_value=(payment, txn))
    svc = _make_service(gateway_sale_recurring_cit=cit)

    recurring, _ = await svc.create_subscription(
        customer_id="CLI-USD",
        amount=5000,
        itbis=0,
        card_number="4260550061845872",
        expiration="203012",
        cvc="123",
        cardholder_name="Jane Doe",
        cardholder_email="jane@ejemplo.com",
        currency="USD",
    )

    # Subscription stores the currency
    assert recurring.currency_code == Currency.USD
    # First charge (Payment passed to the gateway) also carries USD
    charged_payment = cit.call_args[0][0]
    assert charged_payment.currency_code == Currency.USD
    assert charged_payment.currency_code.azul_code == "US$"


@pytest.mark.asyncio
async def test_create_subscription_auto_derives_itbis_from_total():
    """When itbis is omitted, the ITBIS included in the total is derived (18%)."""
    payment = _make_approved_payment("TOKEN-TAX")
    cit = AsyncMock(return_value=(payment, _make_txn(payment.id)))
    svc = _make_service(gateway_sale_recurring_cit=cit)

    recurring, _ = await svc.create_subscription(
        customer_id="CLI-TAX",
        amount=59000,          # RD$590 total, ITBIS incluido
        itbis=None,            # omitido → autocalcular
        card_number="4260550061845872",
        expiration="203012",
        cvc="123",
        cardholder_name="Ana",
        cardholder_email="ana@ejemplo.com",
    )

    assert recurring.amount == 59000          # se cobra el total tal cual
    assert recurring.itbis == 9000            # RD$90 desglosado del total
    charged = cit.call_args[0][0]
    assert charged.amount == 59000 and charged.itbis == 9000


@pytest.mark.asyncio
async def test_create_subscription_rejects_itbis_greater_than_amount():
    """An explicit ITBIS larger than the total is rejected."""
    svc = _make_service(gateway_sale_recurring_cit=AsyncMock())
    with pytest.raises(ValueError, match="itbis"):
        await svc.create_subscription(
            customer_id="CLI-Z", amount=5000, itbis=6000,
            card_number="4260550061845872", expiration="203012", cvc="123",
            cardholder_name="Z", cardholder_email="z@z.com",
        )


@pytest.mark.asyncio
async def test_create_subscription_rejects_unknown_currency():
    """An unsupported currency must be rejected, not silently coerced to DOP."""
    svc = _make_service(gateway_sale_recurring_cit=AsyncMock())

    with pytest.raises(ValueError, match="currency"):
        await svc.create_subscription(
            customer_id="CLI-X",
            amount=5000,
            itbis=0,
            card_number="4260550061845872",
            expiration="203012",
            cvc="123",
            cardholder_name="X",
            cardholder_email="x@x.com",
            currency="EUR",
        )


@pytest.mark.asyncio
async def test_charge_skips_when_azul_already_charged():
    """If Azul already has an APPROVED tx for this order, do NOT charge again.

    Covers the crash window where the card was charged at Azul but the local
    payment row was never saved.
    """
    sub = _make_recurring()
    svc = _make_service(saved_recurring=sub)
    svc._payments.get_by_id = AsyncMock(return_value=None)          # no local record
    svc._gw.verify_payment = AsyncMock(return_value={              # but Azul has it
        "Found": True, "IsoCode": "00", "AzulOrderId": "AZ-EXISTING",
    })
    svc._gw.sale_mit = AsyncMock()

    result = await svc.charge("sub-test-id")

    svc._gw.sale_mit.assert_not_awaited()          # must NOT re-charge
    assert result.status == PaymentStatus.APPROVED
    assert result.azul_order_id == "AZ-EXISTING"
    svc._payments.save.assert_awaited()            # audit record persisted
    svc._recurring.update.assert_awaited_once()    # schedule advanced


@pytest.mark.asyncio
async def test_create_subscription_with_trial():
    """If trial_days > 0, it should tokenize without charging and start a trial."""
    from app.domain.entities import SavedCard
    saved_mock = SavedCard(
        customer_id="CLI-002",
        token="TRIAL-TOKEN",
        card_brand="Visa",
        card_last4="1234",
        expiration="203012",
        is_default=True
    )
    
    gw_create_token = AsyncMock(return_value=saved_mock)
    svc = _make_service(gateway_sale_recurring_cit=AsyncMock())
    svc._gw.create_token = gw_create_token
    svc._card_repo = MagicMock()
    svc._card_repo.save = AsyncMock()

    recurring, initial_payment = await svc.create_subscription(
        customer_id="CLI-002",
        amount=5000,
        itbis=900,
        card_number="4260550061841234",
        expiration="203012",
        cvc="123",
        cardholder_name="Maria Gomez",
        cardholder_email="maria@ejemplo.com",
        trial_days=7,
    )

    # Gateway should have called create_token instead of sale_recurring_cit
    gw_create_token.assert_awaited_once()
    svc._gw.sale_recurring_cit.assert_not_called()  # type: ignore[attr-defined]

    assert initial_payment is None
    assert recurring.data_vault_token == "TRIAL-TOKEN"
    assert recurring.trial_ends_at is not None
    assert recurring.last_charged_at is None
    assert recurring.status == SubscriptionStatus.ACTIVE

    svc._card_repo.save.assert_awaited_once()
    svc._recurring.save.assert_awaited_once()  # type: ignore[attr-defined]


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
    svc._recurring.save.assert_not_awaited()  # type: ignore[attr-defined]
    assert initial_payment is not None
    assert initial_payment.status == PaymentStatus.DECLINED


# ---------------------------------------------------------------------------
# charge (MIT)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_subscription_sends_first_charge_receipt():
    """The first successful charge must send a billing receipt (was missing)."""
    payment = _make_approved_payment("TOK-RCPT")
    cit = AsyncMock(return_value=(payment, _make_txn(payment.id)))
    svc = _make_service(gateway_sale_recurring_cit=cit)

    mock_enviar = AsyncMock(return_value=True)
    with patch("app.services.scheduler.enviar_correo_pago", mock_enviar):
        await svc.create_subscription(
            customer_id="CLI-1", amount=59000, itbis=None,
            card_number="4260550061845872", expiration="203012", cvc="123",
            cardholder_name="Ana", cardholder_email="ana@ejemplo.com",
        )

    mock_enviar.assert_awaited_once()
    kw = mock_enviar.call_args.kwargs
    assert kw["success"] is True
    assert kw["to_email"] == "ana@ejemplo.com"
    assert kw["total"] == 590.0


@pytest.mark.asyncio
async def test_manual_charge_sends_receipt_on_success():
    """Manual charge must notify the customer on success (was silent)."""
    sub = _make_recurring()
    sub.cardholder_email = "c@e.com"
    payment = _make_approved_payment()
    mit = AsyncMock(return_value=(payment, _make_txn(payment.id)))
    svc = _make_service(gateway_sale_mit=mit, saved_recurring=sub)
    svc._payments.get_by_id = AsyncMock(return_value=None)
    svc._gw.verify_payment = AsyncMock(return_value={"Found": False})

    mock_enviar = AsyncMock(return_value=True)
    with patch("app.services.scheduler.enviar_correo_pago", mock_enviar):
        await svc.charge("sub-test-id")

    mock_enviar.assert_awaited_once()
    assert mock_enviar.call_args.kwargs["success"] is True


@pytest.mark.asyncio
async def test_manual_charge_sends_failure_email_on_decline():
    """Manual charge must notify the customer on decline too (was silent)."""
    sub = _make_recurring()
    sub.cardholder_email = "c@e.com"
    declined = Payment(amount=5000, itbis=900, payment_type=PaymentType.RECURRING)
    declined.status = PaymentStatus.DECLINED
    declined.iso_code = IsoCode.DECLINED_FUNDS
    declined.response_message = "Fondos insuficientes"
    mit = AsyncMock(return_value=(declined, _make_txn(declined.id)))
    svc = _make_service(gateway_sale_mit=mit, saved_recurring=sub)
    svc._payments.get_by_id = AsyncMock(return_value=None)
    svc._gw.verify_payment = AsyncMock(return_value={"Found": False})

    mock_enviar = AsyncMock(return_value=True)
    with patch("app.services.scheduler.enviar_correo_pago", mock_enviar):
        await svc.charge("sub-test-id")

    mock_enviar.assert_awaited_once()
    assert mock_enviar.call_args.kwargs["success"] is False


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
    svc._recurring.update.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_charge_mit_declined_does_not_advance_schedule():
    """A declined MIT charge must NOT advance next_charge_at nor mark the cycle paid.

    Regression: previously charge() advanced the schedule unconditionally, so a
    declined card was treated as paid (customer got a free cycle) and no retry
    was scheduled.
    """
    sub = _make_recurring()
    original_next = sub.next_charge_at
    original_last = sub.last_charged_at

    declined = Payment(amount=5000, itbis=900, payment_type=PaymentType.RECURRING)
    declined.status = PaymentStatus.DECLINED
    declined.iso_code = IsoCode.DECLINED_FUNDS
    declined.response_message = "Fondos insuficientes"
    txn = _make_txn(declined.id)

    mit = AsyncMock(return_value=(declined, txn))
    svc = _make_service(gateway_sale_mit=mit, saved_recurring=sub)

    result = await svc.charge("sub-test-id")

    assert result.status == PaymentStatus.DECLINED
    # Retry policy applied — attempt bumped, schedule NOT advanced a full cycle
    assert sub.failed_attempts == 1
    assert sub.status == SubscriptionStatus.ACTIVE  # not yet exhausted
    assert sub.last_charged_at == original_last     # unchanged — nothing was paid
    # next_charge_at moved to the short retry window (~1 day), not +30 days
    now = datetime.now(timezone.utc)
    assert sub.next_charge_at > now
    assert sub.next_charge_at < now + timedelta(days=2)
    assert sub.next_charge_at != original_next
    svc._recurring.update.assert_awaited_once()  # type: ignore[attr-defined]


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
    svc._recurring.pause.return_value = paused  # type: ignore

    result = await svc.pause_subscription("sub-test-id")

    svc._recurring.pause.assert_awaited_once_with("sub-test-id")  # type: ignore[attr-defined]
    assert result.status == SubscriptionStatus.PAUSED


@pytest.mark.asyncio
async def test_resume_subscription():
    """resume_subscription should reset failed_attempts and go ACTIVE."""
    sub = _make_recurring(status=SubscriptionStatus.PAUSED)
    svc = _make_service(saved_recurring=sub)
    resumed = _make_recurring(status=SubscriptionStatus.ACTIVE)
    resumed.failed_attempts = 0
    svc._recurring.resume.return_value = resumed  # type: ignore

    result = await svc.resume_subscription("sub-test-id")

    svc._recurring.resume.assert_awaited_once_with("sub-test-id")  # type: ignore[attr-defined]
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
    svc._consents.save.return_value = consent  # type: ignore[attr-defined]

    result = await svc.record_consent(
        subscription_id="sub-test-id",
        customer_id="CLI-001",
        consent_text="Acepto que se me cobre RD$500 cada 30 días.",
        ip_address="10.0.0.1",
        user_agent="Mozilla/5.0",
    )

    svc._consents.save.assert_awaited_once()  # type: ignore[attr-defined]
    assert result.subscription_id == "sub-test-id"
    assert result.ip_address == "10.0.0.1"
    assert "30 días" in result.consent_text
