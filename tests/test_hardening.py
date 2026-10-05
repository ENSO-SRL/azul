from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
from test_subscription_identity import storage, add_subscription
import pytest
from app.services.access_policy import access_decision
from app.services.billing_attempts import BillingAttempts, BillingConflict

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)
def sub(**kw):
    row=dict(status='ACTIVE',trial_ends_at=None,last_charged_at=None,
             frequency_days=30,data_vault_token='')
    row.update(kw)
    return SimpleNamespace(**row)

def test_active_without_payment_is_denied():
    assert not access_decision([sub()], now=NOW)['allow_access']

def test_retry_date_does_not_grant_access():
    assert not access_decision([sub(last_charged_at=NOW-timedelta(days=31))],now=NOW)['allow_access']

def test_duplicate_expired_trial_cannot_hide_valid_trial():
    result=access_decision([sub(trial_ends_at=NOW-timedelta(days=1)),sub(trial_ends_at=NOW+timedelta(days=1))],now=NOW)
    assert result['allow_access'] and result['requires_review']

def test_paid_period_is_preserved_when_paused():
    assert access_decision([sub(status='PAUSED',last_charged_at=NOW-timedelta(days=2))],now=NOW)['allow_access']

def test_trial_boundary_denies_access():
    assert not access_decision([sub(trial_ends_at=NOW)],now=NOW)['allow_access']

@pytest.mark.asyncio
async def test_missing_persistent_reservation_fails_closed():
    with pytest.raises(BillingConflict):
        await BillingAttempts(None).reserve('cycle','sub','payment')

@pytest.mark.asyncio
async def test_duplicate_reservation_fails_closed():
    from sqlalchemy.exc import IntegrityError
    db=SimpleNamespace(add=lambda x: None,commit=AsyncMock(side_effect=IntegrityError('sql',{},Exception())),rollback=AsyncMock())
    with pytest.raises(BillingConflict):
        await BillingAttempts(db).reserve('cycle','sub','payment')
    db.rollback.assert_awaited_once()

@pytest.mark.asyncio
async def test_persistent_reservation_only_one_concurrent_winner(storage):
    first, second = storage(), storage()
    results = await asyncio.gather(
        BillingAttempts(first).reserve('same-cycle','sub','payment'),
        BillingAttempts(second).reserve('same-cycle','sub','payment'),
        return_exceptions=True)
    assert sum(isinstance(result, BillingConflict) for result in results) == 1
    assert sum(result is None for result in results) == 1

@pytest.mark.asyncio
async def test_uncertain_attempt_survives_new_session(storage):
    first = storage()
    await BillingAttempts(first).reserve('uncertain-cycle','sub','payment')
    await BillingAttempts(first).uncertain('uncertain-cycle')
    with pytest.raises(BillingConflict):
        await BillingAttempts(storage()).reserve('uncertain-cycle','sub','payment')

@pytest.mark.asyncio
async def test_direct_api_rejects_legacy_duplicate_before_gateway(storage):
    from unittest.mock import MagicMock
    from app.infrastructure.repo_impl import SQLRecurringRepository
    from app.services.recurring_service import RecurringService
    db=storage()
    add_subscription(db)
    gateway=MagicMock(create_token=AsyncMock())
    service=RecurringService(MagicMock(),SQLRecurringRepository(db),MagicMock(),gateway,db_session=db)
    with pytest.raises(ValueError, match='CONFLICT'):
        await service.create_subscription('228',50000,9000,'4242424242424242','203012','123',trial_days=7)
    gateway.create_token.assert_not_awaited()

@pytest.mark.asyncio
async def test_concurrent_charge_calls_submit_only_once(storage):
    from app.infrastructure.repo_impl import SQLRecurringRepository
    from app.services.recurring_service import RecurringService
    from app.domain.entities import PaymentStatus
    from app.services import scheduler
    db=storage()
    row=add_subscription(db,'228',token='test-token',days=-1)
    row.card_expiration='203012';db.session.commit()
    async def sale(payment, token):
        await asyncio.sleep(0.01)
        payment.status=PaymentStatus.APPROVED
        return payment,MagicMock()
    gateway=MagicMock(verify_payment=AsyncMock(return_value={'Found':False}),sale_mit=AsyncMock(side_effect=sale))
    payments=MagicMock(get_by_id=AsyncMock(return_value=None),save=AsyncMock())
    services=[RecurringService(payments,SQLRecurringRepository(session),MagicMock(save=AsyncMock()),gateway,db_session=session)
              for session in (db,storage())]
    with patch.object(scheduler,'notify_charge_outcome',AsyncMock()):
        results=await asyncio.gather(*(service.charge(row.id) for service in services),return_exceptions=True)
    assert gateway.sale_mit.await_count==1
    assert sum(isinstance(result,BillingConflict) for result in results)==1

@pytest.mark.asyncio
async def test_timeout_does_not_schedule_another_charge(storage):
    from app.infrastructure.repo_impl import SQLRecurringRepository
    from app.infrastructure.models import BillingAttemptModel
    from app.services.recurring_service import RecurringService
    from sqlalchemy import select
    db=storage()
    row=add_subscription(db,'228',token='test-token',days=-1)
    row.card_expiration='203012';db.session.commit();deadline=row.next_charge_at
    gateway=MagicMock(verify_payment=AsyncMock(return_value={'Found':False}),sale_mit=AsyncMock(side_effect=TimeoutError()))
    service=RecurringService(MagicMock(get_by_id=AsyncMock(return_value=None)),SQLRecurringRepository(db),MagicMock(),gateway,db_session=db)
    with pytest.raises(TimeoutError): await service.charge(row.id)
    assert db.session.execute(select(BillingAttemptModel)).scalar_one().status=='UNCERTAIN'
    assert row.next_charge_at==deadline and row.failed_attempts==0
    with pytest.raises(BillingConflict): await service.charge(row.id)
    assert gateway.sale_mit.await_count==1

@pytest.mark.asyncio
async def test_default_card_replaces_recurring_token_without_changing_dates(storage):
    from app.infrastructure.models import SavedCardModel
    from app.services.token_service import TokenService
    db=storage()
    row=add_subscription(db,'228',token='old-token')
    deadline=row.next_charge_at
    card=SavedCardModel(id='new-card',customer_id='228',token='new-token',expiration='203012',card_last4='4242',card_brand='Visa')
    db.add(card);await db.commit()
    await TokenService(MagicMock(),MagicMock(),db_session=db).set_default_card('228','new-card')
    assert row.data_vault_token=='new-token' and row.next_charge_at==deadline

@pytest.mark.asyncio
async def test_activation_failure_is_persisted_and_retries_without_charging(storage):
    from app.services import post_payment
    from app.infrastructure.models import SubscriptionActivationJobModel
    from test_subscription_identity import payment
    db=storage();add_subscription(db,'228',token='old-token',days=-1)
    paid=payment()
    with patch.object(post_payment,'_activate_subscription',AsyncMock(return_value=post_payment.PostPaymentResult(subscription_error='simulated'))):
        failed=await post_payment.create_subscription_if_needed(paid,'228',db,card_expiration='203012')
    assert failed.subscription_error
    assert (await db.get(SubscriptionActivationJobModel,paid.id)).status=='PENDING'
    result=await post_payment.create_subscription_if_needed(paid,'228',db)
    assert not result.subscription_error
    assert (await db.get(SubscriptionActivationJobModel,paid.id)).status=='DONE'

@pytest.mark.asyncio
async def test_full_monthly_payment_grants_month_once(storage):
    from app.services import post_payment
    from test_subscription_identity import payment, rows
    db=storage();paid=payment()
    result=await post_payment.create_subscription_if_needed(paid,'228',db,card_expiration='203012')
    assert not result.subscription_error and not result.in_trial
    row=rows(db)[0]
    assert row.last_charged_at==paid.created_at
    assert row.next_charge_at==paid.created_at+timedelta(days=30)
    deadline=row.next_charge_at
    await post_payment.create_subscription_if_needed(paid,'228',db)
    assert rows(db)[0].next_charge_at==deadline

def test_pending_activation_is_visible_and_profile_html_is_escaped():
    from routers.checkout import _html_result, _html_form
    html=_html_result('APPROVED','Pago aprobado; activación pendiente. No repitas el pago.','payment',50000,'00')
    assert 'activación pendiente' in html and 'No repitas el pago' in html
    assert '<script data-test>' not in _html_form(prefill_name='<script data-test>')

@pytest.mark.asyncio
async def test_resume_cannot_create_alias_duplicate(storage):
    from app.services.recurring_service import RecurringService
    from app.infrastructure.repo_impl import SQLRecurringRepository
    db=storage();add_subscription(db)
    paused=add_subscription(db,'228',status='PAUSED')
    service=RecurringService(MagicMock(),SQLRecurringRepository(db),MagicMock(),MagicMock(),db_session=db)
    with pytest.raises(ValueError,match='CONFLICT'):
        await service.resume_subscription(paused.id)
