"""Regression cases from the October 8 audit; no network or real charges."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from test_subscription_identity import storage, add_subscription, rows, payment, card, EMAIL
from app.domain.entities import PaymentStatus
from app.infrastructure.models import BillingAttemptModel, SubscriptionActivationJobModel, SavedCardModel
from app.infrastructure.repo_impl import SQLPaymentRepository, SQLRecurringRepository
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.payment_service import PaymentService
from app.services.recurring_service import RecurringService
from app.services.access_policy import access_decision
from app.services import post_payment
from routers import registration, checkout, refunds, threeds


@pytest.mark.asyncio
async def test_incomplete_bank_response_blocks_retry(storage):
    db=storage()
    async def unknown(p,*args,**kwargs):
        p.status=PaymentStatus.DECLINED
        p.iso_code=''
        return p,None
    gw=SimpleNamespace(sale=AsyncMock(side_effect=unknown))
    svc=PaymentService(SQLPaymentRepository(db),SimpleNamespace(save=AsyncMock()),gw)
    from app.services.billing_attempts import BillingConflict
    for _ in range(2):
        with pytest.raises(BillingConflict):
            await svc.process_sale(50000,0,'4242424242424242','209912','123',
                idempotency_key='unknown-response',customer_id='228')
    assert gw.sale.await_count==1
    assert (await db.execute(select(BillingAttemptModel))).scalar_one().status=='UNCERTAIN'


@pytest.mark.asyncio
async def test_read_timeout_does_not_fail_over_and_repeat_post():
    import httpx
    from app.infrastructure.azul_gateway import _post_with_failover
    client=SimpleNamespace(post=AsyncMock(side_effect=httpx.ReadTimeout('lost response')))
    with pytest.raises(httpx.ReadTimeout):
        await _post_with_failover(client,{'test':'no-card-data'},'production')
    assert client.post.await_count==1


def test_callback_signature_is_bound_to_payment():
    from app.services.payment_authorization import callback_signature,verify_callback
    signed=callback_signature('payment-a')
    verify_callback('payment-a',signed)
    with pytest.raises(HTTPException): verify_callback('payment-b',signed)
    with pytest.raises(HTTPException): verify_callback('payment-a','')


def test_uncertain_checkout_does_not_claim_decline_or_offer_another_charge():
    html=checkout._html_result('UNCERTAIN','','',0,'')
    assert 'Pago pendiente de verificación' in html
    assert 'Rechazado' not in html
    assert 'Intentar de nuevo' not in html
    assert 'Usar otra tarjeta' not in html


@pytest.mark.asyncio
async def test_payment_access_rejects_different_account(storage):
    from app.services.payment_authorization import require_payment_access
    db=storage()
    original=payment();original.customer_id='another-account'
    await SQLPaymentRepository(db).save(original)
    with patch('app.services.payment_authorization.decode_user_info_token',return_value={'sub':'228'}):
        with pytest.raises(HTTPException) as exc:
            await require_payment_access(request(),original.id,db)
    assert exc.value.status_code==404


@pytest.mark.asyncio
async def test_uncertain_refund_is_not_retried(storage):
    from app.services.refund_service import refund_payment
    from app.services.billing_attempts import BillingConflict
    db=storage();original=payment();original.azul_order_id='fake-ref'
    original.created_at-=timedelta(days=1)
    await SQLPaymentRepository(db).save(original)
    gw=SimpleNamespace(refund=AsyncMock(side_effect=TimeoutError('lost response')))
    with pytest.raises(TimeoutError): await refund_payment(db,original.id,None,None,gw)
    with pytest.raises(BillingConflict): await refund_payment(db,original.id,None,None,gw)
    assert gw.refund.await_count==1
    assert (await SQLPaymentRepository(db).get_by_id(original.id)).status==PaymentStatus.APPROVED


@pytest.mark.asyncio
async def test_tokenization_failure_never_becomes_a_charge(storage):
    from app.main import app
    from app.infrastructure.database import get_db
    from app.utils.token_utils import require_user_info
    db=storage()
    svc=SimpleNamespace(process_sale=AsyncMock(),process_hold_verify=AsyncMock())
    token_svc=SimpleNamespace(register_card=AsyncMock(side_effect=ValueError('VALIDATION_ERROR:TrxType')))
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_user_info]=lambda:{'sub':'228'}
    app.dependency_overrides[checkout._get_service]=lambda:svc
    app.dependency_overrides[checkout._get_token_svc]=lambda:token_svc
    try:
        with patch('app.utils.token_utils.decode_user_info_token',return_value={'sub':'228'}):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test',
                cookies={'checkout_csrf':'test','user_info':'fixture'}) as client:
                response=await client.post('/checkout/process',data={'card_number':'4242424242424242',
                    'cardholder_name':'Fixture','cardholder_email':EMAIL,'expiration':'12/99','cvc':'123','csrf_token':'test'})
        assert response.status_code==503
        token_svc.register_card.assert_awaited_once()
        svc.process_sale.assert_not_awaited()
        svc.process_hold_verify.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_approved_membership_without_vault_token_keeps_paid_access(storage):
    from app.domain.entities import Payment
    from app.services.payment_lifecycle import begin_payment,complete_payment
    db=storage()
    paid,_=await begin_payment(db,Payment(amount=50000,customer_id='228'),kind='sale',membership=True)
    paid.status=PaymentStatus.APPROVED;paid.iso_code='00'
    await complete_payment(db,paid)
    result=await post_payment.create_subscription_if_needed(paid,'228',db)
    assert result.subscription_created and not result.subscription_error
    assert access_decision(rows(db))['allow_access'] is True
    assert not rows(db)[0].data_vault_token


@pytest.mark.asyncio
async def test_full_refund_revokes_only_its_paid_period(storage):
    from app.services.refund_service import refund_payment
    db=storage();original=payment();original.azul_order_id='fake-order'
    original.created_at-=timedelta(days=1)
    await SQLPaymentRepository(db).save(original)
    sub=add_subscription(db,'228',days=-2);sub.last_charged_at=original.created_at
    await db.commit()
    async def refund(payment,**kwargs):
        payment.status=PaymentStatus.APPROVED;payment.iso_code='00'
        return payment,None
    await refund_payment(db,original.id,None,None,SimpleNamespace(refund=AsyncMock(side_effect=refund)))
    assert not access_decision(rows(db))['allow_access']


@pytest.mark.asyncio
async def test_delayed_activation_preserves_cancellation(storage):
    from app.domain.entities import Payment
    from app.services.payment_lifecycle import begin_payment,complete_payment
    db=storage();sub=add_subscription(db,'228',days=-1)
    paid,_=await begin_payment(db,Payment(amount=50000,customer_id='228'),kind='sale',membership=True)
    sub.status='CANCELLED';await db.commit()
    paid.status=PaymentStatus.APPROVED;paid.iso_code='00';paid.data_vault_token='fake-token'
    await complete_payment(db,paid)
    result=await post_payment.create_subscription_if_needed(paid,'228',db)
    assert not result.subscription_error
    assert len(rows(db))==1 and rows(db)[0].status=='CANCELLED'
    assert access_decision(rows(db))['allow_access'] is True


@pytest.mark.asyncio
async def test_refund_cancels_pending_activation(storage):
    from app.domain.entities import Payment
    from app.services.payment_lifecycle import begin_payment,complete_payment
    from app.services.refund_service import refund_payment
    db=storage()
    paid,_=await begin_payment(db,Payment(amount=50000,customer_id='228'),kind='sale',membership=True)
    paid.status=PaymentStatus.APPROVED;paid.iso_code='00';paid.azul_order_id='fake-ref'
    paid.data_vault_token='fake-token'
    await complete_payment(db,paid)
    await refund_payment(db,paid.id,None,None,SimpleNamespace(void=AsyncMock(return_value={'IsoCode':'00'})))
    result=await post_payment.create_subscription_if_needed(paid,'228',db)
    assert result.subscription_error and not rows(db)
    assert (await db.get(SubscriptionActivationJobModel,paid.id)).status=='CANCELLED'


def request():
    return Request({'type':'http','method':'POST','path':'/checkout/pay-with-token',
                    'headers':[(b'cookie',b'user_info=fixture')],'query_string':b''})


async def token_checkout(db, gateway):
    saved=card()
    token_svc=SimpleNamespace(list_cards=AsyncMock(return_value=[saved]),set_default_card=AsyncMock())
    with patch('app.utils.token_utils.decode_user_info_token',return_value={'sub':'228','email':EMAIL}), \
         patch('app.infrastructure.azul_gateway.AzulPaymentGateway',return_value=gateway), \
         patch('app.infrastructure.repo_impl.SQLTransactionRepository',return_value=SimpleNamespace(save=AsyncMock())), \
         patch.object(post_payment,'handle_post_payment_actions',AsyncMock()):
        return await checkout.pay_with_token(request(),card_id=saved.id,csrf_token='fixture',checkout_csrf='fixture',
                svc=MagicMock(),token_svc=token_svc,db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[PaymentStatus.APPROVED,PaymentStatus.DECLINED])
async def test_checkout_finishes_the_durable_attempt(storage,status):
    db=storage(); sub=add_subscription(db,'228',token='vault-test',days=-1)
    sub.card_expiration='203012';await db.commit()
    async def sale(p,token):
        p.status=status;p.iso_code='00' if status==PaymentStatus.APPROVED else '51'
        return p,MagicMock()
    gateway=SimpleNamespace(sale_cit=AsyncMock(side_effect=sale))
    response=await token_checkout(db,gateway)
    assert response.status_code==200
    attempt=(await db.execute(select(BillingAttemptModel))).scalar_one()
    assert attempt.status==status.value, 'Completed checkout leaves RESERVED instead of terminal status'


@pytest.mark.asyncio
async def test_prior_trial_cannot_be_reissued_after_cancellation(storage):
    db=storage(); add_subscription(db,'228',days=-10,status='CANCELLED')
    body=registration.RegistrationRequest(email=EMAIL,name='Audit',last_name='Fixture')
    try: await registration.register_trial(body,db)
    except HTTPException as exc: assert exc.status_code==409
    assert len(rows(db))==1, 'A returning account received a fresh 30-day trial'


@pytest.mark.asyncio
async def test_paid_paused_period_prevents_checkout_charge(storage):
    db=storage(); sub=add_subscription(db,'228',token='vault-test',days=-10,status='PAUSED')
    sub.last_charged_at=datetime.now(timezone.utc)-timedelta(days=1);await db.commit()
    assert access_decision([sub])['allow_access'] is True
    async def sale(p,token):
        p.status=PaymentStatus.DECLINED;return p,MagicMock()
    gw=SimpleNamespace(sale_cit=AsyncMock(side_effect=sale))
    await token_checkout(db,gw)
    gw.sale_cit.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_idempotency_key_does_not_submit_two_sales(storage):
    # Force two requests to read absence before either gateway call completes.
    calls=[]
    async def sale(p,*args,**kwargs):
        calls.append(p.id)
        await asyncio.sleep(0.03)
        p.status=PaymentStatus.APPROVED;return p,MagicMock()
    repo=SQLPaymentRepository(storage())
    gw=SimpleNamespace(sale=AsyncMock(side_effect=sale))
    svc=PaymentService(repo,SimpleNamespace(save=AsyncMock()),gw)
    services=[PaymentService(SQLPaymentRepository(storage()),SimpleNamespace(save=AsyncMock()),gw) for _ in range(2)]
    results=await asyncio.gather(*(service.process_sale(50000,9000,'4242424242424242','203012','123',
        idempotency_key='same-business-request',customer_id='228') for service in services),return_exceptions=True)
    assert sum(isinstance(r,Exception) for r in results)==1
    assert len(calls)==1, 'Identical idempotency key reached gateway twice with different payment IDs'


@pytest.mark.asyncio
async def test_declined_void_does_not_claim_cancellation(storage):
    original=payment();original.azul_order_id='fixture-azul'
    repo=SQLPaymentRepository(storage());await repo.save(original)
    gateway=SimpleNamespace(void=AsyncMock(return_value={'IsoCode':'05','ResponseMessage':'Declined'}))
    result=await refunds.cancel_payment(original.id,refunds.RefundRequest(),(repo,MagicMock(),gateway))
    assert (await repo.get_by_id(original.id)).status==PaymentStatus.APPROVED and result['status']!='CANCELLED', 'Rejected void marked paid record cancelled'


@pytest.mark.asyncio
async def test_repeated_full_refund_is_not_submitted_again(storage):
    original=payment();original.azul_order_id='fixture-azul';original.created_at-=timedelta(days=1)
    async def refund(payment,**kwargs):
        payment.status=PaymentStatus.APPROVED;payment.iso_code='00';return payment,MagicMock()
    repo=SQLPaymentRepository(storage());await repo.save(original)
    gw=SimpleNamespace(refund=AsyncMock(side_effect=refund))
    for _ in range(2):
        try: await refunds.cancel_payment(original.id,refunds.RefundRequest(),(repo,SimpleNamespace(save=AsyncMock()),gw))
        except HTTPException as exc: assert exc.status_code==409
    assert gw.refund.await_count==1, 'Repeated full refund submitted twice without local balance/idempotency guard'


@pytest.mark.asyncio
async def test_approved_checkout_survives_failure_before_activation_job(storage):
    db=storage()
    async def sale(p,*args,**kwargs):
        p.status=PaymentStatus.APPROVED;p.data_vault_token='vault-test';return p,MagicMock()
    svc=PaymentService(SQLPaymentRepository(db),SimpleNamespace(save=AsyncMock(side_effect=RuntimeError('simulated transaction-log failure'))),
                       SimpleNamespace(sale_recurring_cit=AsyncMock(side_effect=sale)))
    result=await svc.process_sale(50000,9000,'4242424242424242','203012','123',save_card=True,customer_id='228',subscription_checkout=True)
    assert result.status==PaymentStatus.APPROVED
    jobs=(await db.execute(select(SubscriptionActivationJobModel))).scalars().all()
    assert len(jobs)==1, 'Approved payment persisted but activation retry has no durable work item'


@pytest.mark.asyncio
async def test_new_checkout_card_respects_existing_default_under_email(storage):
    db=storage()
    db.add(SavedCardModel(id='legacy-default',customer_id=EMAIL,token='old-token',is_default=True));await db.commit()
    async def sale(p,*args,**kwargs):
        p.status=PaymentStatus.APPROVED;p.data_vault_token='new-token';return p,MagicMock()
    svc=PaymentService(SQLPaymentRepository(db),SimpleNamespace(save=AsyncMock()),
                       SimpleNamespace(sale_recurring_cit=AsyncMock(side_effect=sale)),card_repo=SQLSavedCardRepository(db))
    await svc.process_sale(50000,9000,'4242424242424242','203012','123',save_card=True,customer_id='228',idempotency_key='test-card')
    cards=(await db.execute(select(SavedCardModel).where(SavedCardModel.is_default.is_(True)))).scalars().all()
    assert len(cards)==1, 'Card saved through PaymentService bypasses group identity lookup and creates second default'


@pytest.mark.asyncio
async def test_3ds_status_requires_authorization_and_hides_vault_token():
    from app.main import app
    paid=payment();paid.data_vault_token='FAKE-SECRET-TOKEN'
    svc=SimpleNamespace(get_payment=AsyncMock(return_value=paid))
    app.dependency_overrides[threeds._get_service]=lambda:svc
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://audit.local') as client:
            response=await client.get(f'/api/v1/3ds/{paid.id}/status')
        if response.status_code==200:
            assert response.json()['data_vault_token']=='FAKE-SECRET-TOKEN'
        assert response.status_code in (401,403,404), 'Unauthenticated status exposes gateway vault token for a known payment ID'
    finally: app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_declined_initial_subscription_allows_reviewed_new_attempt(storage):
    db=storage()
    async def sale(p,*args,**kwargs):
        p.status=PaymentStatus.DECLINED;p.iso_code='51';return p,MagicMock()
    gw=SimpleNamespace(sale_recurring_cit=AsyncMock(side_effect=sale))
    svc=RecurringService(SimpleNamespace(save=AsyncMock()),SQLRecurringRepository(db),SimpleNamespace(save=AsyncMock()),gw,db_session=db)
    await svc.create_subscription('228',50000,9000,'4242424242424242','203012','123')
    attempt=(await db.execute(select(BillingAttemptModel))).scalar_one()
    assert attempt.status=='DECLINED' and attempt.payment_id, 'Initial rejected payment leaves create:<user>:initial reserved without its payment ID'


@pytest.mark.asyncio
async def test_certification_runner_is_not_public():
    from app.main import app
    from app.routers import cert
    calls=[]
    async def fake_run(run_id,base_url):
        calls.append(run_id)
        yield 'event: done\ndata: {}\n\n'
    with patch.object(cert,'_run_tests',fake_run):
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://audit.local') as client:
            response=await client.get('/cert/stream/audit-fake-run')
    cert._sessions.pop('audit-fake-run',None)
    assert response.status_code in (401,403,404) and not calls, 'Anonymous GET invoked certification runner; real runner uses configured payment gateway'


@pytest.mark.asyncio
async def test_reconciliation_uses_id_sent_to_gateway():
    from app.services.reconciliation_service import ReconciliationService
    pm=SimpleNamespace(id='gateway-custom-order',idempotency_key='caller-business-key',iso_code='00',status='APPROVED')
    result=MagicMock();result.scalars.return_value.all.return_value=[pm]
    db=SimpleNamespace(execute=AsyncMock(return_value=result),add=MagicMock(),commit=AsyncMock())
    service=ReconciliationService(db)
    service._gw=SimpleNamespace(verify_payment=AsyncMock(return_value={'Found':False}))
    await service.run()
    service._gw.verify_payment.assert_awaited_once_with(pm.id)
