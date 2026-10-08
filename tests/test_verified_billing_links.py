"""Existing, independently addressable accounts need explicit billing ownership."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, select

from test_subscription_identity import storage, add_subscription, EMAIL, UUID, checkout_app, checkout_request, rows, card
from test_historical_billing_identity import service
from app.domain.entities import SavedCard
from app.infrastructure.models import PaymentModel, SavedCardModel
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.subscription_identity import resolve_customer_identity, CustomerIdentityError
from app.services.token_service import TokenService
from routers import tokens, registration, checkout

SOURCE_EMAIL = 'old-profile@example.invalid'
SOURCE_UUID = 'fe86ebf2-60ef-491f-aa62-711673cf1e55'


def reviewed_link(db, *, linked=True):
    db.session.execute(text("INSERT INTO public.users (id,email,uuid) VALUES (233,:email,:uuid)"),
                       {'email': SOURCE_EMAIL, 'uuid': SOURCE_UUID})
    if linked:
        db.session.execute(text("INSERT INTO pagos.customer_identity_links VALUES (233,228,'reviewed-identity-and-approved-payment','test-reviewer')"))
    db.session.commit()


def paid_subscription(db):
    sub = add_subscription(db, '233', token='synthetic-existing-token', days=-20)
    sub.last_charged_at = datetime.now(timezone.utc) - timedelta(days=1)
    sub.next_charge_at = sub.last_charged_at + timedelta(days=30)
    db.session.commit()
    return sub


@pytest.mark.asyncio
@pytest.mark.parametrize('identifier', ['228','233',EMAIL,UUID,SOURCE_EMAIL,SOURCE_UUID])
async def test_paid_period_visible_from_both_profiles_without_rewriting_history(storage, identifier):
    db=storage(); reviewed_link(db); sub=paid_subscription(db)
    db.add(PaymentModel(id='approved-source-payment', customer_id='233', amount=50000,
                        itbis=9000, payment_type='SALE', status='APPROVED'))
    db.session.commit()
    svc, gateway=service(db)
    identity=await resolve_customer_identity(db, identifier)
    bot=await svc.get_customer_status(identifier)
    web=await tokens.get_user_payment_status(identifier,db)
    assert identity.customer_id == web['user_info']['customer_id'] == '228'
    assert bot['allow_access'] is web['summary']['allow_access'] is True
    assert bot['reason']=='paid' and bot['valid_until']==sub.next_charge_at.isoformat()
    assert web['summary']['valid_until']==bot['valid_until']
    assert not bot['requires_review'] and bot['active_count']==1
    assert any(p['id']=='approved-source-payment' for p in web['recent_payments'])
    assert sub.customer_id=='233' and sub.amount==50000
    gateway.sale_mit.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_account_without_review_does_not_share_entitlement(storage):
    db=storage(); reviewed_link(db,linked=False); paid_subscription(db)
    svc,_=service(db)
    assert (await svc.get_customer_status('228'))['reason']=='no_subscription'
    assert (await svc.get_customer_status('233'))['reason']=='paid'


@pytest.mark.asyncio
async def test_third_account_collision_fails_closed_for_both_profiles(storage):
    db=storage(); reviewed_link(db)
    db.session.execute(text("INSERT INTO public.users (id,email,uuid) VALUES (444,:email,'third-uuid')"),{'email':SOURCE_EMAIL.upper()})
    db.session.commit()
    for identifier in ('228','233',SOURCE_EMAIL):
        with pytest.raises(CustomerIdentityError):
            await resolve_customer_identity(db,identifier)


@pytest.mark.asyncio
async def test_missing_links_table_is_operational_failure_not_no_subscription(storage):
    db=storage();db.session.execute(text('DROP TABLE pagos.customer_identity_links'));db.session.commit()
    with pytest.raises(Exception,match='customer_identity_links'):
        await service(db)[0].get_customer_status('228')


@pytest.mark.asyncio
async def test_historical_alias_owned_by_third_account_blocks_whole_group(storage):
    db=storage();reviewed_link(db)
    db.session.execute(text("INSERT INTO public.users (id,email,uuid) VALUES (444,'other@example.invalid','other-uuid')"))
    db.session.execute(text("INSERT INTO pagos.customer_identity_aliases VALUES ('999',444,'other-review','test')"))
    db.session.execute(text("UPDATE public.users SET email='999' WHERE id=233"))
    db.session.commit()
    for identifier in ('228','233','999'):
        with pytest.raises(CustomerIdentityError):await resolve_customer_identity(db,identifier)


@pytest.mark.asyncio
async def test_malformed_restored_chain_fails_closed(storage):
    db=storage();reviewed_link(db)
    db.session.execute(text("INSERT INTO public.users (id,email,uuid) VALUES (444,'other@example.invalid','other-uuid')"))
    db.session.execute(text("INSERT INTO pagos.customer_identity_links VALUES (228,444,'invalid-chain','test')"))
    db.session.commit()
    for identifier in ('228','233','444'):
        with pytest.raises(CustomerIdentityError):await resolve_customer_identity(db,identifier)


@pytest.mark.asyncio
async def test_card_registration_uses_primary_identity_and_shared_default_card(storage):
    db=storage();reviewed_link(db);paid_subscription(db)
    db.add(SavedCardModel(id='existing',customer_id='233',token='old-token',card_last4='0000',expiration='209912',is_default=True))
    db.session.commit()
    gateway=MagicMock(create_token=AsyncMock(return_value=SavedCard(id='new-card',customer_id='228',token='new-token',
                           card_brand='Visa',card_last4='1111',expiration='209912')))
    svc=TokenService(SQLSavedCardRepository(db),gateway,db)
    await svc.register_card('233','0000000000001111','209912','000')
    assert gateway.create_token.call_args.kwargs['customer_id']=='228'
    cards=(await db.execute(select(SavedCardModel))).scalars().all()
    assert {c.id for c in cards}=={'existing','new-card'}
    assert [c.id for c in cards if c.is_default]==['existing']


@pytest.mark.asyncio
async def test_deleting_old_card_cancels_linked_subscription_after_canonicalization(storage):
    db=storage();reviewed_link(db);sub=paid_subscription(db)
    sub.customer_id='228'
    db.add(SavedCardModel(id='existing',customer_id='233',token=sub.data_vault_token,card_last4='0000',expiration='209912',is_default=True))
    db.session.commit();gateway=MagicMock(delete_token=AsyncMock())
    svc=TokenService(SQLSavedCardRepository(db),gateway,db)
    await svc.delete_card_by_id('existing','228')
    gateway.delete_token.assert_awaited_once_with('synthetic-existing-token')
    assert rows(db)[0].status=='CANCELLED'


@pytest.mark.asyncio
async def test_concurrent_registration_from_both_emails_reuses_paid_membership(storage):
    setup=storage();reviewed_link(setup);sub=paid_subscription(setup)
    original=(sub.id,sub.amount,sub.last_charged_at,sub.next_charge_at,sub.data_vault_token)
    responses=await asyncio.gather(*(
        registration.register_trial(registration.RegistrationRequest(email=email,name='Fixture',last_name='User'),storage())
        for email in (EMAIL,SOURCE_EMAIL)))
    saved=rows(storage())
    assert len(saved)==1
    assert (saved[0].id,saved[0].amount,saved[0].last_charged_at,saved[0].next_charge_at,saved[0].data_vault_token)==original
    assert saved[0].customer_id=='228'
    assert all(r.status=='already_active' and r.customer_id=='228' for r in responses)


@pytest.mark.asyncio
@pytest.mark.parametrize('session_id',['228','233'])
async def test_old_session_and_primary_session_reuse_saved_card_without_sale(storage,session_id):
    db=storage();reviewed_link(db);sub=paid_subscription(db);deadline=sub.next_charge_at
    db.add(SavedCardModel(id='old-card',customer_id=' '+SOURCE_EMAIL.upper()+' ',token='synthetic-existing-token',
                         card_brand='Visa',card_last4='0000',expiration='209912',is_default=True))
    db.session.commit()
    gateway=MagicMock(delete_token=AsyncMock(),create_token=AsyncMock(),sale_cit=AsyncMock())
    token_svc=TokenService(SQLSavedCardRepository(db),gateway,db)
    payment_svc=AsyncMock()
    assert [c.id for c in await token_svc.list_cards(session_id)]==['old-card']
    with patch('app.utils.token_utils.decode_user_info_token',return_value={'sub':session_id}), \
         patch('app.infrastructure.azul_gateway.AzulPaymentGateway',return_value=gateway):
        app=checkout_app(db,token_svc,payment_svc)
        del app.dependency_overrides[checkout._get_token_svc]
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test',
                               cookies={'checkout_csrf':'csrf'}) as client:
            response=await client.post('/checkout/pay-with-token',data={'csrf_token':'csrf','card_id':'old-card'})
    assert response.status_code==200 and 'No se realizó un cobro' in response.text
    assert len(rows(db))==1 and rows(db)[0].next_charge_at==deadline
    gateway.sale_cit.assert_not_awaited();gateway.create_token.assert_not_awaited()
    payment_svc.process_sale.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_profile_cannot_delete_linked_card(storage):
    db=storage();reviewed_link(db)
    db.add(SavedCardModel(id='other-card',customer_id='unrelated',token='unrelated-token',card_last4='0000',expiration='209912'))
    db.session.commit();gateway=MagicMock(delete_token=AsyncMock())
    svc=TokenService(SQLSavedCardRepository(db),gateway,db)
    with pytest.raises(PermissionError):await svc.delete_card_by_id('other-card','228')
    gateway.delete_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_cannot_use_posted_primary_id_without_valid_session(storage):
    db=storage();reviewed_link(db);paid_subscription(db);token_svc,payment_svc=AsyncMock(),AsyncMock()
    with patch('app.utils.token_utils.decode_user_info_token',return_value=None):
        async with AsyncClient(transport=ASGITransport(app=checkout_app(db,token_svc,payment_svc)),base_url='http://test',
                               cookies={'checkout_csrf':'csrf'}) as client:
            response=await client.post('/checkout/process',data={'csrf_token':'csrf','customer_id':'228','card_number':'0000000000000000',
                'cardholder_name':'Fixture','cardholder_email':'unrelated@example.invalid','expiration':'12/30','cvc':'000'})
    assert response.status_code==401
    token_svc.register_card.assert_not_awaited();payment_svc.process_sale.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('identifier',['228','233'])
async def test_checkout_page_shows_same_paid_period_even_with_later_cancelled_row(storage,identifier):
    db=storage();reviewed_link(db);sub=paid_subscription(db)
    add_subscription(db,'228',status='CANCELLED')
    app=checkout_app(db,AsyncMock(list_cards=AsyncMock(return_value=[])),AsyncMock())
    with patch('app.utils.token_utils.decode_user_info_token',return_value={'sub':identifier}):
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            response=await client.get('/checkout')
    assert response.status_code==200
    assert 'Membresía pagada vigente' in response.text and sub.next_charge_at.strftime('%d/%m/%Y') in response.text
    assert 'Sin membresía activa' not in response.text


@pytest.mark.asyncio
async def test_checkout_page_does_not_label_expired_active_row_as_paid(storage):
    db=storage();reviewed_link(db);add_subscription(db,'233',days=-2)
    app=checkout_app(db,AsyncMock(list_cards=AsyncMock(return_value=[])),AsyncMock())
    with patch('app.utils.token_utils.decode_user_info_token',return_value={'sub':'228'}):
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            response=await client.get('/checkout')
    assert response.status_code==200 and 'Prueba vencida' in response.text
    assert 'Membresía pagada vigente' not in response.text


@pytest.mark.asyncio
async def test_paid_customer_adding_card_is_not_told_they_are_in_expired_trial(storage):
    db=storage();reviewed_link(db);sub=paid_subscription(db);deadline=sub.next_charge_at
    token_svc,payment_svc=AsyncMock(),AsyncMock();token_svc.register_card.return_value=card()
    with patch('app.services.post_payment._trigger_confirmation_email',new=AsyncMock(return_value=True)):
        response=await checkout_request(db,token_svc,payment_svc)
    assert response.status_code==200 and 'se conserva tu período pagado' in response.text
    assert 'Período de prueba' not in response.text
    assert rows(db)[0].next_charge_at==deadline
    payment_svc.process_sale.assert_not_awaited()
