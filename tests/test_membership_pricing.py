"""Pricing regressions with real local storage and no payment network calls."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.domain.entities import Payment, PaymentStatus
from app.infrastructure import azul_gateway
from app.infrastructure.models import BillingAttemptModel
from app.services import post_payment
from app.services.payment_lifecycle import begin_payment, complete_payment
from routers import registration
from test_subscription_identity import (
    storage, card, rows, EMAIL, add_subscription, checkout_request,
)


@pytest.mark.asyncio
async def test_registration_and_tokenization_save_total_including_itbis(storage):
    db = storage()
    await registration.register_trial(registration.RegistrationRequest(
        email=EMAIL, name='Test', last_name='User'), db)
    assert (rows(db)[0].amount, rows(db)[0].itbis) == (59000, 9000)


@pytest.mark.asyncio
async def test_verification_amount_cannot_become_subscription_price(storage):
    db = storage()
    with patch('app.services.post_payment._trigger_confirmation_email', new=AsyncMock(return_value=True)):
        result = await post_payment.create_trial_subscription('228', card(), 100, 0, EMAIL, db)
    assert result.subscription_created
    assert (rows(db)[0].amount, rows(db)[0].itbis) == (59000, 9000)


@pytest.mark.asyncio
async def test_checkout_sends_590_total_and_90_itbis(storage, monkeypatch):
    db = storage()
    add_subscription(db, '228', days=-10)
    service, tokens = AsyncMock(), AsyncMock()
    service.process_sale.return_value = Payment(amount=59000, itbis=9000,
                                                status=PaymentStatus.DECLINED)
    await checkout_request(db, tokens, service)
    args = service.process_sale.call_args.kwargs
    assert (args['amount'], args['itbis']) == (59000, 9000)
    monkeypatch.setattr(azul_gateway, 'load_azul_config', lambda: SimpleNamespace(merchant_id='test'))
    payload = azul_gateway.AzulPaymentGateway()._base_payload(
        Payment(amount=args['amount'], itbis=args['itbis']))
    assert (payload['Amount'], payload['Itbis']) == ('59000', '9000')


@pytest.mark.asyncio
async def test_new_underpayment_cannot_activate_membership(storage):
    db = storage()
    paid, _ = await begin_payment(db, Payment(amount=50000, customer_id='228'),
                                 kind='sale', membership=True)
    paid.status = PaymentStatus.APPROVED
    await complete_payment(db, paid)
    result = await post_payment.create_subscription_if_needed(paid, '228', db)
    assert result.subscription_error
    assert not rows(db)


@pytest.mark.asyncio
async def test_pre_fix_approved_500_keeps_paid_access_without_extra_charge(storage):
    db = storage()
    paid, _ = await begin_payment(db, Payment(amount=50000, itbis=9000, customer_id='228'),
                                 kind='sale', membership=True)
    attempt = (await db.execute(select(BillingAttemptModel))).scalar_one()
    # Reproduce the durable record written by the currently deployed revision.
    context = json.loads(attempt.context_json)
    context.pop('amount'); context.pop('itbis')
    attempt.context_json = json.dumps(context)
    await db.commit()
    paid.status = PaymentStatus.APPROVED
    await complete_payment(db, paid)
    result = await post_payment.create_subscription_if_needed(paid, '228', db)
    assert result.subscription_created and not result.subscription_error
    assert rows(db)[0].last_charged_at is not None
    assert rows(db)[0].amount == 59000


@pytest.mark.asyncio
async def test_callback_uses_price_captured_before_gateway(storage, monkeypatch):
    db = storage()
    paid, _ = await begin_payment(db, Payment(amount=59000, itbis=9000, customer_id='228'),
                                 kind='checkout_token', membership=True)
    monkeypatch.setattr(post_payment, 'MEMBERSHIP_AMOUNT', 70000)
    paid.status = PaymentStatus.APPROVED
    await complete_payment(db, paid)
    result = await post_payment.create_subscription_if_needed(paid, '228', db)
    assert result.subscription_created and not result.subscription_error
    assert (rows(db)[0].amount, rows(db)[0].itbis) == (59000, 9000)
