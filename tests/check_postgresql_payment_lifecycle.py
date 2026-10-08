"""Real PostgreSQL races with fake bank responses, in a disposable loopback DB.

Run with TEST_LOCAL_PG_PORT set. No production URL or payment gateway is used.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.schema import CreateTable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = int(os.environ['TEST_LOCAL_PG_PORT'])
DATABASE = 'codex_payment_lifecycle_' + uuid.uuid4().hex
DSN = f'postgresql://codex_test@127.0.0.1:{PORT}/{DATABASE}'
os.environ.update(DATABASE_URL=DSN.replace('postgresql://', 'postgresql+asyncpg://'),
                  DB_SSL='disable', AWS_EC2_METADATA_DISABLED='true', API_KEY='local-test-only', AZUL_ENV='sandbox')

from app.domain.entities import Payment, PaymentStatus
from app.infrastructure.models import (PaymentModel, RecurringPaymentModel, SavedCardModel,
    BillingAttemptModel, SubscriptionActivationJobModel, TransactionModel)
from app.infrastructure.repo_impl import SQLPaymentRepository, SQLRecurringRepository
from app.services.billing_attempts import BillingConflict
from app.services.payment_service import PaymentService
from app.services.payment_lifecycle import begin_payment, complete_payment
from app.services.payment_recovery import recover_payment_operations
from app.services.post_payment import create_subscription_if_needed
from app.services.refund_service import refund_payment
from app.services.recurring_service import RecurringService
from routers import registration


async def main():
    admin = conn = engine = None
    created = False
    checks = []
    try:
        admin = await asyncpg.connect(host='127.0.0.1', port=PORT, user='codex_test', database='postgres', timeout=5)
        assert DATABASE.startswith('codex_payment_lifecycle_') and DATABASE.replace('_', '').isalnum()
        await admin.execute(f'CREATE DATABASE "{DATABASE}"')
        created = True
        conn = await asyncpg.connect(DSN, timeout=5)
        await conn.execute('''CREATE SCHEMA pagos;
            CREATE TABLE public.users(id int PRIMARY KEY, uuid uuid, email text, name text, last_name text,
                phone text, is_active bool, is_confirmed bool, is_cancelled bool, parent_id int,
                bsuid_meta text, created_at timestamptz, updated_at timestamptz);
            INSERT INTO public.users VALUES (228, 'd9e1c417-43ea-407d-9ad1-d0e6f58b5b82',
                'fixture@example.invalid','Fixture','Person','+18095550000',true,true,false,null,null,now(),now());''')
        for model in (PaymentModel, RecurringPaymentModel, SavedCardModel, BillingAttemptModel, SubscriptionActivationJobModel, TransactionModel):
            await conn.execute(str(CreateTable(model.__table__).compile(dialect=postgresql.dialect())))
        await conn.execute('ALTER TABLE pagos.payments ADD COLUMN atlas_user_id integer REFERENCES public.users(id)')
        for filename in ('20261007_customer_identity_aliases.sql', '20261008_verified_billing_links.sql',
                         '20261008_payment_operation_context.sql', '20261008_payment_operation_context.sql'):
            await conn.execute((ROOT/'migrations'/filename).read_text(encoding='utf-8'))
        checks.append('schema migrations apply and operation migration is idempotent')
        engine = create_async_engine(os.environ['DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def reset():
            await conn.execute('TRUNCATE pagos.transactions,pagos.billing_attempts,pagos.subscription_activation_jobs,pagos.payments,pagos.recurring_payments,pagos.saved_cards')

        async def sale(p, *args, **kwargs):
            await asyncio.sleep(.07)
            p.status = PaymentStatus.APPROVED
            p.iso_code = '00'
            p.azul_order_id = 'fake-bank-reference'
            p.data_vault_token = 'fake-token'
            return p, None

        gw = SimpleNamespace(sale=AsyncMock(side_effect=sale), sale_recurring_cit=AsyncMock(side_effect=sale))
        async def pay(key, amount=50000, membership=False):
            async with sessions() as db:
                svc = PaymentService(SQLPaymentRepository(db), SimpleNamespace(save=AsyncMock()), gw, db_session=db)
                return await svc.process_sale(amount, 0, '4242424242424242', '209912', '123',
                    idempotency_key=key, customer_id='228', subscription_checkout=membership)

        results = await asyncio.gather(pay('same-request'), pay('same-request'), return_exceptions=True)
        assert gw.sale.await_count == 1
        assert sum(isinstance(r, BillingConflict) for r in results) == 1
        original = next(r for r in results if isinstance(r, Payment))
        assert (await pay('same-request')).id == original.id and gw.sale.await_count == 1
        try:
            await pay('same-request', 60000)
        except BillingConflict:
            pass
        else:
            raise AssertionError('changed amount accepted for same key')
        checks.append('two concurrent API requests: one bank call; replay returns same payment; changed amount rejected')

        async with sessions() as db:
            result = await create_subscription_if_needed(original, '228', db)
            assert not result.subscription_created
        assert await conn.fetchval('SELECT count(*) FROM pagos.subscription_activation_jobs') == 0
        checks.append('a service payment cannot accidentally activate membership')

        await reset()
        gw.sale.reset_mock()
        results = await asyncio.gather(pay('checkout-a', membership=True), pay('checkout-b', membership=True), return_exceptions=True)
        assert gw.sale.await_count == 1 and sum(isinstance(r, BillingConflict) for r in results) == 1
        approved = next(r for r in results if isinstance(r, Payment))
        assert await conn.fetchval("SELECT status FROM pagos.billing_attempts") == 'APPROVED'
        assert await conn.fetchval("SELECT status FROM pagos.subscription_activation_jobs") == 'PENDING'
        async with sessions() as db:
            activation = await create_subscription_if_needed(approved, '228', db)
            assert activation.subscription_created and not activation.subscription_error
        expiry = await conn.fetchval('SELECT next_charge_at FROM pagos.recurring_payments')
        async with sessions() as db:
            await create_subscription_if_needed(approved, '228', db)
        assert expiry == await conn.fetchval('SELECT next_charge_at FROM pagos.recurring_payments')
        assert await conn.fetchval('SELECT count(*) FROM pagos.recurring_payments') == 1
        checks.append('concurrent membership checkouts produce one payment/job/subscription; activation replay preserves expiry')

        # A crash after bank approval leaves a durable intent; recovery only verifies it.
        await reset()
        async with sessions() as db:
            pending, _ = await begin_payment(db, Payment(amount=50000, customer_id='228'), kind='sale',
                membership=True, context={'card_expiration': '209912'})
        await conn.execute("UPDATE pagos.billing_attempts SET updated_at=now()-interval '10 minutes'")
        verify = SimpleNamespace(verify_payment=AsyncMock(return_value={'Found': True, 'IsoCode':'00',
            'Amount':99999, 'CurrencyPosCode':'$', 'CustomOrderId':pending.id,'AzulOrderId':'fake-order'}))
        async with sessions() as db:
            await recover_payment_operations(db, verify)
        assert await conn.fetchval('SELECT status FROM pagos.payments') == 'PENDING'
        verify.verify_payment.return_value['Amount'] = 50000
        verify.verify_payment.return_value['DataVaultToken'] = 'fake-token'
        await conn.execute("UPDATE pagos.billing_attempts SET updated_at=now()-interval '10 minutes'")
        async with sessions() as db:
            await recover_payment_operations(db, verify)
        assert await conn.fetchval('SELECT status FROM pagos.payments') == 'APPROVED'
        assert await conn.fetchval('SELECT status FROM pagos.subscription_activation_jobs') == 'PENDING'
        checks.append('crash recovery uses verification only; mismatched amount stays blocked; matching proof queues activation')

        await reset()
        timeout_gw = SimpleNamespace(sale=AsyncMock(side_effect=TimeoutError('simulated lost response')))
        async with sessions() as db:
            svc = PaymentService(SQLPaymentRepository(db), SimpleNamespace(save=AsyncMock()), timeout_gw, db_session=db)
            try:
                await svc.process_sale(50000,0,'4242424242424242','209912','123',idempotency_key='timeout',customer_id='228')
            except TimeoutError:
                pass
        assert await conn.fetchval('SELECT status FROM pagos.billing_attempts') == 'UNCERTAIN'
        try:
            await pay('timeout')
        except BillingConflict:
            pass
        else:
            raise AssertionError('uncertain payment was resent')
        checks.append('lost bank response persists UNCERTAIN; new session cannot resubmit')

        await reset()
        async with sessions() as db:
            p = Payment(amount=50000, customer_id='228', status=PaymentStatus.PENDING_3DS_METHOD, azul_order_id='fake-order')
            await SQLPaymentRepository(db).save(p)
        async def method(**kwargs):
            await asyncio.sleep(.07)
            return {'IsoCode':'00','DataVaultToken':'fake-token'}
        method_gw = SimpleNamespace(process_three_ds_method=AsyncMock(side_effect=method))
        async def continue_method():
            async with sessions() as db:
                svc = PaymentService(SQLPaymentRepository(db), SimpleNamespace(save=AsyncMock()), method_gw, db_session=db)
                return await svc.continue_three_ds_method(p.id)
        results = await asyncio.gather(continue_method(), continue_method(), return_exceptions=True)
        assert method_gw.process_three_ds_method.await_count == 1
        assert sum(isinstance(r, BillingConflict) for r in results) == 1
        assert (await continue_method()).status == PaymentStatus.APPROVED
        async with sessions() as db:
            p.status = PaymentStatus.PENDING_3DS_METHOD
            await complete_payment(db, p)
        assert await conn.fetchval('SELECT status FROM pagos.payments') == 'APPROVED'
        checks.append('concurrent 3DS continuation calls bank once; replay and stale responses cannot downgrade approval')

        await reset()
        async def register():
            async with sessions() as db:
                return await registration.register_trial(registration.RegistrationRequest(
                    email='fixture@example.invalid',name='Fixture',last_name='Person'),db)
        results = await asyncio.gather(register(),register())
        assert await conn.fetchval('SELECT count(*) FROM pagos.recurring_payments') == 1
        await conn.execute("UPDATE pagos.recurring_payments SET status='CANCELLED',trial_ends_at=now()-interval '1 day'")
        from fastapi import HTTPException
        try:
            await register()
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError('returning user got a new trial')
        checks.append('concurrent registration creates one trial; cancellation cannot reset trial history')

        await reset()
        now=datetime.now(timezone.utc)
        async with sessions.begin() as db:
            db.add(RecurringPaymentModel(id='renewal-fixture',customer_id='228',status='ACTIVE',
                amount=50000,itbis=0,data_vault_token='fake-token',card_expiration='209912',
                frequency_days=30,last_charged_at=now-timedelta(days=31),next_charge_at=now-timedelta(days=1)))
        gw.sale.reset_mock()
        gw.sale_mit=AsyncMock(side_effect=sale)
        gw.verify_payment=AsyncMock(return_value={'Found':False})
        async def scheduled_charge():
            async with sessions() as db:
                svc=RecurringService(SQLPaymentRepository(db),SQLRecurringRepository(db),
                    SimpleNamespace(save=AsyncMock()),gw,db_session=db)
                return await svc.charge('renewal-fixture')
        with patch('app.services.scheduler.notify_charge_outcome',AsyncMock()):
            results=await asyncio.gather(scheduled_charge(),pay('manual-checkout',membership=True),return_exceptions=True)
        assert gw.sale.await_count+gw.sale_mit.await_count==1, results
        assert sum(isinstance(r,Payment) and r.status==PaymentStatus.APPROVED for r in results)==1, results
        assert await conn.fetchval('SELECT count(*) FROM pagos.payments')==1
        checks.append('scheduler and checkout racing for the same expired cycle produce only one approved charge')

        # Force the opposite ordering: scheduler is in the bank call first.
        await reset()
        async with sessions.begin() as db:
            db.add(RecurringPaymentModel(id='renewal-fixture',customer_id='228',status='ACTIVE',
                amount=50000,itbis=0,data_vault_token='fake-token',card_expiration='209912',
                frequency_days=30,last_charged_at=now-timedelta(days=31),next_charge_at=now-timedelta(days=1)))
        entered=asyncio.Event()
        release=asyncio.Event()
        async def held_mit(p,*args):
            entered.set()
            await release.wait()
            return await sale(p)
        gw.sale_mit=AsyncMock(side_effect=held_mit)
        with patch('app.services.scheduler.notify_charge_outcome',AsyncMock()):
            worker=asyncio.create_task(scheduled_charge())
            try:
                await asyncio.wait_for(entered.wait(),3)
                try:
                    await pay('checkout-during-renewal',membership=True)
                except BillingConflict:
                    pass
                else:
                    raise AssertionError('checkout bypassed renewal intent')
            finally:
                release.set()
            renewed=await worker
        assert renewed.status==PaymentStatus.APPROVED
        assert await conn.fetchval('SELECT next_charge_at FROM pagos.recurring_payments')==renewed.created_at+timedelta(days=30)
        assert await conn.fetchval('SELECT status FROM pagos.subscription_activation_jobs')=='DONE'
        checks.append('scheduler-first ordering blocks checkout and commits one renewal with exact expiry and completed job')

        await reset()
        async with sessions.begin() as db:
            db.add(RecurringPaymentModel(id='renewal-fixture',customer_id='228',status='ACTIVE',
                amount=50000,itbis=0,data_vault_token='fake-token',card_expiration='209912',
                frequency_days=30,last_charged_at=now-timedelta(days=31),next_charge_at=now-timedelta(days=1)))
        async def decline(p,*args):
            p.status=PaymentStatus.DECLINED;p.iso_code='51'
            return p,None
        gw.sale_mit=AsyncMock(side_effect=decline)
        with patch('app.services.scheduler.notify_charge_outcome',AsyncMock()):
            declined=await scheduled_charge()
        assert declined.status==PaymentStatus.DECLINED
        assert await conn.fetchval('SELECT failed_attempts FROM pagos.recurring_payments')==1
        assert await conn.fetchval('SELECT next_charge_at>now() FROM pagos.recurring_payments')
        assert await conn.fetchval('SELECT status FROM pagos.billing_attempts')=='DECLINED'
        checks.append('definitive renewal decline records one failure and schedules backoff without granting paid access')

        await conn.execute("UPDATE pagos.payments SET created_at=now()-interval '120 days'")
        async with sessions() as db:
            await SQLPaymentRepository(db).save(Payment(amount=100,status=PaymentStatus.DECLINED,
                created_at=now-timedelta(days=120)))
        from app.services.scheduler import _purge_old_transactions
        await _purge_old_transactions(sessions)
        assert await conn.fetchval('SELECT count(*) FROM pagos.payments')==1
        assert await conn.fetchval('SELECT id FROM pagos.payments')==declined.id
        checks.append('retention preserves declined payments referenced by the durable operation ledger')

        await reset()
        original = Payment(amount=50000,customer_id='228',status=PaymentStatus.APPROVED,
            azul_order_id='fake-original',created_at=datetime.now(timezone.utc)-timedelta(days=1))
        async with sessions() as db:
            await SQLPaymentRepository(db).save(original)
        async def refund_response(payment, **kwargs):
            return await sale(payment)
        refund_gw = SimpleNamespace(refund=AsyncMock(side_effect=refund_response))
        async def refund(amount,key):
            async with sessions() as db:
                return await refund_payment(db,original.id,amount,key,refund_gw)
        results = await asyncio.gather(refund(30000,'partial-a'),refund(30000,'partial-b'),return_exceptions=True)
        assert refund_gw.refund.await_count == 1 and sum(isinstance(r,BillingConflict) for r in results)==1
        assert sum(isinstance(r,dict) and r['status']=='REFUNDED' for r in results)==1, results
        await refund(20000,'remaining')
        assert await conn.fetchval('SELECT status FROM pagos.payments WHERE id=$1',original.id)=='REFUNDED'
        assert refund_gw.refund.await_count==2
        checks.append('concurrent partial refunds cannot exceed balance; full returned balance marks original refunded')

        report = {'postgresql':await conn.fetchval('SHOW server_version'),'environment':'disposable loopback DB',
                  'production_contacted':False,'real_payments_made':False,'checks':checks}
        destination = os.environ.get('TEST_PAYMENT_LIFECYCLE_REPORT')
        if destination:
            Path(destination).write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))
    finally:
        if engine: await engine.dispose()
        if conn: await conn.close()
        if admin:
            if created: await admin.execute(f'DROP DATABASE "{DATABASE}"')
            await admin.close()


if __name__=='__main__':
    with patch('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('External HTTP forbidden in local validation'))):
        asyncio.run(main())
