"""Actual PostgreSQL + both HTTP services, with fake bank and fake SendGrid.

TEST_LOCAL_PG_PORT must point to a disposable loopback cluster.
USER_SERVICE_SOURCE must point to the matching local ATLAS-BACKEND checkout.
Creates/drops only a randomly named test database. Never sends a real email.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import asyncpg
from fastapi import FastAPI, Header, HTTPException
import httpx
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
PORT=int(os.environ['TEST_LOCAL_PG_PORT'])
DATABASE='codex_invoice_'+uuid.uuid4().hex
DSN=f'postgresql://codex_test@127.0.0.1:{PORT}/{DATABASE}'
os.environ.update(DATABASE_URL=DSN.replace('postgresql://','postgresql+asyncpg://'),DB_SSL='disable',
    AWS_EC2_METADATA_DISABLED='true',AZUL_ENV='sandbox',API_KEY='offline-only',
    ATLAS_API_BASE_URL='http://users.test',USER_SERVICE_KEY='local-service-key',PAYMENT_EMAIL_ENABLED='1',
    SENDGRID_API_KEY='test-no-real-send',MAIL_FROM='sender@example.com',ACCOUNTING_INVOICE_EMAIL='accountant@example.com')
# Fail closed if an accidental external provider or production DB is used.
original_connect=socket.socket.connect
def loopback_only(sock,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1'):
        raise AssertionError('External network disabled for invoice integration tests')
    return original_connect(sock,address)
socket.socket.connect=loopback_only

from app.infrastructure.database import Base
from app.infrastructure.models import InvoiceModel,InvoiceEmailModel,PaymentModel,BillingAttemptModel
from app.domain.entities import Payment,PaymentStatus,PaymentType,Currency
from app.infrastructure.repo_impl import SQLPaymentRepository
from app.services.payment_service import PaymentService
from app.services.payment_lifecycle import complete_payment
from app.services import invoice_delivery as invoices
from routers.checkout import _html_result

source=Path(os.environ['USER_SERVICE_SOURCE']).resolve()
spec=importlib.util.spec_from_file_location('checkout_delivery_under_test',source/'services/invoices/checkout_delivery.py')
backend=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=backend
spec.loader.exec_module(backend)


async def main():
    admin=engine=None
    checks=[]
    try:
        admin=await asyncpg.connect(host='127.0.0.1',port=PORT,user='codex_test',database='postgres',timeout=5)
        assert DATABASE.startswith('codex_invoice_') and DATABASE.replace('_','').isalnum()
        await admin.execute(f'CREATE DATABASE "{DATABASE}"')
        engine=create_async_engine(os.environ['DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        async with engine.begin() as c:
            await c.execute(text('CREATE SCHEMA pagos'))
            for _ in range(2):
                for migration in (ROOT/'migrations/20261125_invoice_delivery.sql',source/'migrations/sql/20261125_checkout_email_delivery.sql'):
                    # asyncpg execute accepts a migration batch; SQLAlchemy uses prepared statements.
                    raw=await c.get_raw_connection()
                    await raw.driver_connection.execute(migration.read_text(encoding='utf-8'))
            await c.run_sync(Base.metadata.create_all)
        checks.append('additive migrations apply twice on actual PostgreSQL')

        async def session_dep():
            async with sessions() as session:
                yield session
        async def auth(x_service_key: str=Header(...,alias='x-service-key')):
            if x_service_key!='local-service-key':
                raise HTTPException(401,'invalid key')
        api=FastAPI()
        api.include_router(backend.receipt_router(session_dep,auth),prefix='/invoices/service')
        OriginalClient=httpx.AsyncClient
        provider_calls=[]
        provider_plans={}
        lose_ack=set()

        async def fake_sendgrid(request):
            body=json.loads(request.content)
            key=body['personalizations'][0]['custom_args']['notification_id']
            provider_calls.append(body)
            plan=provider_plans.get(key,[])
            result=plan.pop(0) if plan else 202
            if isinstance(result,Exception):
                raise result
            await asyncio.sleep(.02)
            return httpx.Response(result,headers={'X-Message-Id':'provider-'+key})

        class UsersTransport(httpx.ASGITransport):
            async def handle_async_request(self,request):
                response=await super().handle_async_request(request)
                body=json.loads(request.content) if request.content else {}
                key=body.get('notification_id')
                if key in lose_ack:
                    lose_ack.remove(key)
                    raise httpx.ReadTimeout('user service accepted but caller lost response')
                return response

        def client_factory(*args,**kwargs):
            # Distinguish user-service HTTP from its SendGrid HTTP.
            class RoutedTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self,request):
                    if request.url.host=='users.test':
                        return await UsersTransport(app=api).handle_async_request(request)
                    assert request.url.host=='api.sendgrid.com'
                    return await fake_sendgrid(request)
            return OriginalClient(*args,transport=RoutedTransport(),**kwargs)

        bank_calls=[]
        async def fake_bank(p,*args,**kwargs):
            bank_calls.append(p.id)
            p.status=PaymentStatus.APPROVED;p.iso_code='00'
            return p,None
        bank=SimpleNamespace(sale=AsyncMock(side_effect=fake_bank))

        async def pay(key,*,email='customer@example.com',currency=Currency.DOP):
            async with sessions() as db:
                svc=PaymentService(SQLPaymentRepository(db),SimpleNamespace(save=AsyncMock()),bank,db_session=db)
                return await svc.process_sale(59000,9000,'4242424242424242','209912','123',
                    order_id='CHK-'+key,customer_id='228',cardholder_name='Cliente de prueba',
                    cardholder_email=email,currency=currency.value,idempotency_key=key)

        async def due(delivery_id):
            async with sessions() as db:
                await db.execute(update(InvoiceEmailModel).where(InvoiceEmailModel.id==delivery_id)
                    .values(next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1),lease_until=None))
                await db.commit()

        with patch.object(httpx,'AsyncClient',client_factory):
            p=await pay('direct')
            assert await invoices.dispatch_invoice(p.id,sessions)=='ACCEPTED'
            async with sessions() as db:
                invoice=(await db.execute(select(InvoiceModel).where(InvoiceModel.payment_id==p.id))).scalar_one()
                assert (invoice.subtotal,invoice.itbis,invoice.total,invoice.balance_due)==(50000,9000,59000,0)
                assert invoice.pdf_content.startswith(b'%PDF-')
                original_pdf=invoice.pdf_content
                job=await db.get(InvoiceEmailModel,f'invoice:{p.id}:customer')
                snapshot=invoices.email_payload(invoice,job)
                with patch.object(invoices,'render_text',side_effect=AssertionError('template changed')):
                    assert invoices.email_payload(invoice,job)==snapshot
            assert len(provider_calls)==2 and len(bank_calls)==1
            assert {b['personalizations'][0]['to'][0]['email'] for b in provider_calls}=={'customer@example.com','accountant@example.com'}
            assert all(len(b['personalizations'][0]['to'])==1 and 'cc' not in b['personalizations'][0] for b in provider_calls)
            assert all(b['attachments'][0]['type']=='application/pdf' for b in provider_calls)
            replay=await pay('direct')
            await invoices.dispatch_invoice(replay.id,sessions)
            assert len(bank_calls)==1 and len(provider_calls)==2
            checks.append('approved checkout commits one immutable invoice/PDF and two isolated recipients; replay never recharges or re-mails')

            # Repeated final 3DS callbacks preserve the same paid invoice.
            async with sessions() as db:
                await complete_payment(db,p)
            async with sessions() as db:
                same=(await db.execute(select(InvoiceModel).where(InvoiceModel.payment_id==p.id))).scalar_one()
                assert same.pdf_content==original_pdf
            await invoices.dispatch_invoice(p.id,sessions)
            assert len(provider_calls)==2
            checks.append('duplicate final callback preserves invoice number, issue date, PDF and delivery IDs')

            same_email=await pay('same-recipient',email='accountant@example.com')
            before=len(provider_calls)
            assert await invoices.dispatch_invoice(same_email.id,sessions)=='ACCEPTED'
            assert len(provider_calls)==before+1
            checks.append('identical customer/accountant address is sent once; persisted payload survives template changes')

            p2=await pay('accounting-retry')
            accounting=f'invoice:{p2.id}:accounting'
            provider_plans[accounting]=[429,202]
            assert await invoices.dispatch_invoice(p2.id,sessions)=='ACCEPTED'
            await due(accounting)
            await invoices.retry_invoice_emails(sessions)
            sent_ids=[b['personalizations'][0]['custom_args']['notification_id'] for b in provider_calls]
            assert sent_ids.count(f'invoice:{p2.id}:customer')==1 and sent_ids.count(accounting)==2
            assert len(bank_calls)==3
            checks.append('accounting 429 retries after restart without repeating successful customer email or charge')

            p3=await pay('lost-api-response')
            delivery=f'invoice:{p3.id}:customer'
            lose_ack.add(delivery)
            assert await invoices.dispatch_invoice(p3.id,sessions)=='RETRYABLE'
            await due(delivery)
            assert await invoices.dispatch_one(sessions,delivery)=='ACCEPTED'
            sent_ids=[b['personalizations'][0]['custom_args']['notification_id'] for b in provider_calls]
            assert sent_ids.count(delivery)==1
            checks.append('lost user-service HTTP acknowledgement retries the same ID; SendGrid is called once')

            p4=await pay('provider-timeout')
            delivery=f'invoice:{p4.id}:customer'
            provider_plans[delivery]=[httpx.ReadTimeout('provider acknowledgement lost')]
            assert await invoices.dispatch_invoice(p4.id,sessions)=='UNCERTAIN'
            await due(delivery)
            await invoices.retry_invoice_emails(sessions)
            sent_ids=[b['personalizations'][0]['custom_args']['notification_id'] for b in provider_calls]
            assert sent_ids.count(delivery)==1
            html=_html_result('APPROVED','','fixture',59000,'00',cardholder_email='customer@example.com',receipt_status='UNCERTAIN')
            assert 'Estamos verificando' in html and 'aceptada para envío' not in html
            checks.append('ambiguous provider timeout is held without duplicate email or false sent message')

            p5=await pay('parallel')
            delivery=f'invoice:{p5.id}:customer'
            await asyncio.gather(*(invoices.dispatch_one(sessions,delivery) for _ in range(8)))
            sent_ids=[b['personalizations'][0]['custom_args']['notification_id'] for b in provider_calls]
            assert sent_ids.count(delivery)==1
            async with sessions() as db:
                job=await db.get(InvoiceEmailModel,delivery)
                invoice=await db.get(InvoiceModel,job.invoice_id)
                payload=invoices.email_payload(invoice,job)
            clients=[OriginalClient(transport=httpx.ASGITransport(app=api),base_url='http://users.test') for _ in range(4)]
            try:
                replies=await asyncio.gather(*(c.post('/invoices/service/checkout_receipt',json=payload,
                    headers={'x-service-key':'local-service-key','Idempotency-Key':delivery}) for c in clients))
                assert all(r.status_code==200 and r.json()['status']=='ACCEPTED' for r in replies)
                changed={**payload,'email':'other@example.com'}
                r=await clients[0].post('/invoices/service/checkout_receipt',json=changed,
                    headers={'x-service-key':'local-service-key','Idempotency-Key':delivery})
                assert r.status_code==409
                r=await clients[0].post('/invoices/service/checkout_receipt',json=payload,
                    headers={'x-service-key':'wrong','Idempotency-Key':delivery})
                assert r.status_code==401
            finally:
                for client in clients:
                    await client.aclose()
            sent_ids=[b['personalizations'][0]['custom_args']['notification_id'] for b in provider_calls]
            assert sent_ids.count(delivery)==1
            checks.append('8 parallel outbox workers + parallel HTTP replays send once; altered recipient conflicts; invalid service key rejected')

            p_race=await pay('backend-race')
            delivery=f'invoice:{p_race.id}:customer'
            async with sessions() as db:
                job=await db.get(InvoiceEmailModel,delivery); invoice=await db.get(InvoiceModel,job.invoice_id)
                invoices.prepare_document(invoice)
                payload=invoices.email_payload(invoice,job)
                await db.commit()
            async with OriginalClient(transport=httpx.ASGITransport(app=api),base_url='http://users.test') as client:
                replies=await asyncio.gather(*(client.post('/invoices/service/checkout_receipt',json=payload,
                    headers={'x-service-key':'local-service-key','Idempotency-Key':delivery}) for _ in range(8)))
            assert all(r.status_code==200 and r.json()['status'] in ('SENDING','ACCEPTED') for r in replies)
            sent_ids=[b['personalizations'][0]['custom_args']['notification_id'] for b in provider_calls]
            assert sent_ids.count(delivery)==1
            assert await invoices.dispatch_one(sessions,delivery)=='ACCEPTED'
            checks.append('8 initial concurrent user-service requests claim one provider send, independently of the outbox lock')

            p6=await pay('no-customer-email',email='')
            before=len(provider_calls)
            assert await invoices.dispatch_invoice(p6.id,sessions)=='NOT_REQUESTED'
            assert len(provider_calls)==before+1
            checks.append('invoice and accounting copy survive a missing customer email without claiming customer delivery')

            p7=await pay('usd',currency=Currency.USD)
            async with sessions() as db:
                invoice=(await db.execute(select(InvoiceModel).where(InvoiceModel.payment_id==p7.id))).scalar_one()
                invoices.prepare_document(invoice)
                assert invoice.currency=='USD' and 'US$ 590.00' in invoice.html_content
                await db.commit()
            checks.append('currency and integer-cent tax split persist correctly')

            # MIT builds currency_code without setting the legacy dollar sign.
            recurring=Payment(amount=11800,itbis=1800,customer_id='228',currency_code=Currency.USD,
                status=PaymentStatus.APPROVED,iso_code='00',payment_type=PaymentType.RECURRING,
                cardholder_email='customer@example.com',card_number_masked='****4242')
            async with sessions() as db:
                row=await invoices.record_invoice(db,recurring,{'kind':'subscription','membership':True})
                assert row.currency=='USD' and row.total==11800 and row.subtotal==10000
                await db.commit()
            assert await invoices.dispatch_invoice(recurring.id,sessions)=='ACCEPTED'
            checks.append('recurring subscription invoice uses authoritative ISO currency, customer email and persisted amount')

            # A known crash before committing the provider acknowledgement must not resend.
            delivery=f'invoice:{p7.id}:customer'
            async with sessions() as db:
                job=await db.get(InvoiceEmailModel,delivery); invoice=await db.get(InvoiceModel,job.invoice_id)
                payload=invoices.email_payload(invoice,job)
                digest=__import__('hashlib').sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
                await db.execute(backend.deliveries.insert().values(notification_id=delivery,payload_hash=digest,
                    status='SENDING',lease_until=datetime.now(timezone.utc)-timedelta(minutes=5),updated_at=datetime.now(timezone.utc)))
                await db.commit()
            before=len(provider_calls)
            assert await invoices.dispatch_one(sessions,delivery)=='UNCERTAIN'
            assert len(provider_calls)==before
            checks.append('crash during provider call recovers as uncertain and never blindly sends again')

            # Another environment cannot claim a job addressed to this user service.
            delivery=f'invoice:{p7.id}:accounting'
            with patch.dict(os.environ,{'ATLAS_API_BASE_URL':'http://other-environment.test'}):
                assert await invoices.dispatch_one(sessions,delivery)=='PENDING'
            checks.append('QA/production outbox workers cannot claim another environment scope')

            async with sessions() as db:
                for status,ptype,context in [(PaymentStatus.DECLINED,PaymentType.SALE,{'membership':True}),
                    (PaymentStatus.APPROVED,PaymentType.REFUND,{'membership':True}),
                    (PaymentStatus.APPROVED,PaymentType.SALE,{'kind':'sale','verification_only':True,'membership':True})]:
                    noinvoice=Payment(amount=100,itbis=0,status=status,payment_type=ptype)
                    assert await invoices.record_invoice(db,noinvoice,context) is None
            checks.append('declines, refunds and verification holds cannot generate paid invoices')

            p_render=await pay('render-error')
            with patch.object(invoices,'render_pdf',side_effect=RuntimeError('simulated renderer failure')):
                assert await invoices.dispatch_invoice(p_render.id,sessions)=='RETRYABLE'
            async with sessions() as db:
                assert (await db.get(PaymentModel,p_render.id)).status=='APPROVED'
                row=(await db.execute(select(InvoiceModel).where(InvoiceModel.payment_id==p_render.id))).scalar_one()
                assert row.document_json and not row.pdf_content
            for audience in ('customer','accounting'):
                await due(f'invoice:{p_render.id}:{audience}')
            assert await invoices.dispatch_invoice(p_render.id,sessions)=='ACCEPTED'
            sent_ids=[b['personalizations'][0]['custom_args']['notification_id'] for b in provider_calls]
            assert sent_ids.count(f'invoice:{p_render.id}:customer')==1
            assert sent_ids.count(f'invoice:{p_render.id}:accounting')==1
            checks.append('PDF rendering failure preserves approved payment and draft; repair retries document and both emails once')

        print(json.dumps({'passed':len(checks),'checks':checks,'real_charges':0,'real_emails':0},indent=2))
    finally:
        if engine:
            await engine.dispose()
        if admin:
            await admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
            await admin.close()

asyncio.run(main())
