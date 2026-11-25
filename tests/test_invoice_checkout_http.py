"""Browser routes preserve the persisted delivery result across refresh/3DS."""
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import pytest
from httpx import AsyncClient,ASGITransport
from sqlalchemy import select

from test_subscription_identity import storage,add_subscription,card
from app.domain.entities import PaymentStatus
from app.infrastructure.models import InvoiceModel,InvoiceEmailModel,PaymentModel
from app.infrastructure.repo_impl import SQLPaymentRepository
from app.services.payment_service import PaymentService
from app.services import invoice_delivery,post_payment
from app.infrastructure.database import get_db
from app.utils.token_utils import require_user_info
from routers import checkout


@pytest.mark.asyncio
@pytest.mark.parametrize('route',['direct','token','3ds'])
@pytest.mark.parametrize('state',['ACCEPTED','RETRYABLE','FAILED','UNCERTAIN'])
async def test_checkout_and_result_reload_show_actual_delivery_state(storage,route,state):
    from app.main import app
    db=storage()
    add_subscription(db,'228',token='fixture-only-token',days=-1)
    async def bank_approve(payment,*args,**kwargs):
        payment.status=PaymentStatus.APPROVED;payment.iso_code='00'
        payment.data_vault_token='fixture-only-token';payment.card_number_masked='****4242'
        return payment,None
    async def bank_start(payment,*args,**kwargs):
        if route=='3ds':
            payment.status=PaymentStatus.PENDING_3DS_METHOD
            payment.iso_code='3D2METHOD';payment.azul_order_id='fixture-bank-id'
            payment.threeds_method_form='<form action="https://example.invalid/3ds"></form>'
            return payment,None
        return await bank_approve(payment,*args,**kwargs)
    bank=SimpleNamespace(sale_recurring_cit=AsyncMock(side_effect=bank_start),sale_cit=AsyncMock(side_effect=bank_approve),
        process_three_ds_method=AsyncMock(return_value={'IsoCode':'00','DataVaultToken':'fixture-only-token'}))
    svc=PaymentService(SQLPaymentRepository(db),SimpleNamespace(save=AsyncMock()),bank,db_session=db)
    saved=card()
    token_svc=SimpleNamespace(list_cards=AsyncMock(return_value=[saved]))
    async def dispatch(payment_id):
        job=await db.get(InvoiceEmailModel,f'invoice:{payment_id}:customer')
        assert job is not None
        job.status=state
        await db.commit()
        return state
    mail_dispatch=AsyncMock(side_effect=dispatch)
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[require_user_info]=lambda:{'sub':'228','email':'fixture@example.com'}
    app.dependency_overrides[checkout._get_service]=lambda:svc
    app.dependency_overrides[checkout._get_token_svc]=lambda:token_svc
    try:
        with patch('app.utils.token_utils.decode_user_info_token',return_value={'sub':'228','email':'fixture@example.com'}), \
             patch('app.infrastructure.azul_gateway.AzulPaymentGateway',return_value=bank), \
             patch('app.services.payment_authorization.decode_user_info_token',return_value={'sub':'228'}), \
             patch.object(invoice_delivery,'dispatch_invoice',mail_dispatch), \
             patch.object(post_payment,'_trigger_confirmation_email',AsyncMock(return_value=False)):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test',
                    cookies={'checkout_csrf':'fixture-csrf','user_info':'fixture'}) as client:
                if route in ('direct','3ds'):
                    response=await client.post('/checkout/process',data={'card_number':'4242424242424242',
                        'cardholder_name':'Cliente Fixture','cardholder_email':'fixture@example.com',
                        'expiration':'12/99','cvc':'123','csrf_token':'fixture-csrf'})
                    if route=='3ds':
                        assert response.status_code==200
                        assert (await db.execute(select(InvoiceModel))).scalar_one_or_none() is None
                        pending=(await db.execute(select(PaymentModel))).scalar_one()
                        response=await client.post('/checkout/3ds-continue',data={'payment_id':pending.id,'method_status':'RECEIVED'})
                        assert response.status_code==200,response.text
                        response=await client.get(response.json()['result_url'])
                else:
                    response=await client.post('/checkout/pay-with-token',data={'card_id':saved.id,'csrf_token':'fixture-csrf'})
                assert response.status_code==200,response.text[:1000]
                invoice=(await db.execute(select(InvoiceModel))).scalar_one()
                reload=await client.get('/checkout/result/'+invoice.payment_id)
                assert reload.status_code==200,reload.text[:1000]
                expected={'ACCEPTED':'Factura aceptada para envío','RETRYABLE':'Factura pendiente de envío',
                    'FAILED':'No pudimos enviar la factura','UNCERTAIN':'Estamos verificando el envío'}[state]
                assert expected in response.text and expected in reload.text
                assert 'Recibo enviado' not in response.text+reload.text
                assert (await svc.get_payment(invoice.payment_id)).status==PaymentStatus.APPROVED
                assert bank.sale_cit.await_count+bank.sale_recurring_cit.await_count==1
                assert bank.process_three_ds_method.await_count==(1 if route=='3ds' else 0)
                mail_dispatch.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()
