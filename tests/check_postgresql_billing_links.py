"""Disposable loopback PostgreSQL validation. Never accepts production credentials."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid

import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
PORT=int(os.environ['TEST_LOCAL_PG_PORT'])
DATABASE='codex_billing_link_'+uuid.uuid4().hex
DSN=f'postgresql://codex_test@127.0.0.1:{PORT}/{DATABASE}'
os.environ.update(DATABASE_URL=DSN.replace('postgresql://','postgresql+asyncpg://'),
                  DB_SSL='disable',AWS_EC2_METADATA_DISABLED='true',API_KEY='local-test-only',AZUL_ENV='sandbox')
from app.infrastructure.models import (PaymentModel,RecurringPaymentModel,SavedCardModel,
                                       BillingAttemptModel,SubscriptionActivationJobModel)
from app.infrastructure.repo_impl import SQLRecurringRepository
from app.services.recurring_service import RecurringService
from app.services.subscription_identity import resolve_customer_identity,lock_customer_subscriptions
from routers import tokens,registration

SUB='3b71d6bb-316c-4d04-b87c-829c38cd0ef5'
OLD='69c38cd1-4886-4f79-b5a7-ae48b8a11e30'
PAY='20e0266d-fa07-41f0-b149-7d84de0e0176'
PAID=datetime.fromisoformat('2026-10-08T15:59:34.450182+00:00')
UNTIL=datetime.fromisoformat('2026-11-07T15:59:34.450182+00:00')
NOW=datetime(2026,10,8,18,tzinfo=timezone.utc)

async def main():
    admin=conn=engine=None;created=False;checks=[]
    try:
        admin=await asyncpg.connect(host='127.0.0.1',port=PORT,user='codex_test',database='postgres',timeout=5)
        assert DATABASE.startswith('codex_billing_link_') and DATABASE.replace('_','').isalnum()
        await admin.execute(f'CREATE DATABASE "{DATABASE}"');created=True
        conn=await asyncpg.connect(DSN,timeout=5)
        engine=create_async_engine(os.environ['DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        await conn.execute('''CREATE SCHEMA pagos;
        CREATE TABLE public.users (id integer PRIMARY KEY,uuid uuid,email text,name text,last_name text,
          phone text,is_active boolean,is_confirmed boolean,is_cancelled boolean,parent_id integer,
          bsuid_meta text,created_at timestamptz,updated_at timestamptz);
        INSERT INTO public.users VALUES
          (133,'ab95ff7a-0e9d-46c0-b553-90be40d2f655','primary@example.invalid','Danilo','Bobadilla','+18095550000',true,true,false,null,'fake-bsuid',
            '2026-07-24 11:45:59.588167+00',now()),
          (233,'a63e51d7-bfe0-45df-84aa-4f069c62d7b8','source@example.invalid','Danilo','Bobadilla','+18095550000_duplicado_233',false,true,false,null,null,
            '2026-08-21 02:35:17.273336+00',now()),
          (444,'6c3d6fc6-3c69-4704-bac1-67bfc18dacfe','other@example.invalid','Other','Person','+18095550001',true,true,false,null,null,now(),now());
        CREATE TABLE public.reservas(id int PRIMARY KEY,user_id int REFERENCES public.users(id));
        INSERT INTO public.reservas VALUES (1,133),(2,233);
        ''')
        for model in (PaymentModel,RecurringPaymentModel,SavedCardModel,BillingAttemptModel,SubscriptionActivationJobModel):
            await conn.execute(str(CreateTable(model.__table__).compile(dialect=postgresql.dialect())))
        await conn.execute('ALTER TABLE pagos.payments ADD COLUMN atlas_user_id integer REFERENCES public.users(id)')
        async with sessions.begin() as db:
            db.add(PaymentModel(id=PAY,customer_id='233',amount=50000,itbis=9000,currency='$',status='APPROVED',
                                payment_type='SALE',created_at=PAID,data_vault_token='fake-existing-token',iso_code='00'))
            db.add(RecurringPaymentModel(id=SUB,customer_id='233',amount=50000,itbis=9000,status='ACTIVE',frequency_days=30,
                   data_vault_token='fake-existing-token',card_expiration='209912',last_charged_at=PAID,next_charge_at=UNTIL,
                   trial_ends_at=datetime(2026,9,20,tzinfo=timezone.utc)))
            db.add(RecurringPaymentModel(id=OLD,customer_id='source@example.invalid',amount=200,status='CANCELLED'))
            db.add(SavedCardModel(id='existing-card',customer_id='233',token='fake-existing-token',card_brand='Visa',
                                 card_last4='0000',expiration='209912',is_default=True))
        await conn.execute((ROOT/'migrations/20261007_customer_identity_aliases.sql').read_text(encoding='utf-8'))
        schema=(ROOT/'migrations/20261008_verified_billing_links.sql').read_text(encoding='utf-8')
        await conn.execute(schema);await conn.execute(schema)
        checks.append('empty schema migration is idempotent; no account is automatically linked')
        review=(ROOT/'operations/danilo_233_to_133_review.sql').read_text(encoding='utf-8')
        apply=(ROOT/'operations/danilo_233_to_133_apply.sql').read_text(encoding='utf-8')
        await conn.execute(review)
        assert await conn.fetchval('SELECT count(*) FROM pagos.customer_identity_links')==0
        checks.append('review succeeds and rolls back the link')
        async def rejected(sql):
            try:await conn.execute(sql)
            except (asyncpg.RaiseError,asyncpg.CheckViolationError,asyncpg.UniqueViolationError):
                await conn.execute('ROLLBACK');return
            raise AssertionError('Expected guarded rejection')
        async def untouched():
            assert await conn.fetchval('SELECT count(*) FROM pagos.customer_identity_links')==0
            assert await conn.fetchval('SELECT customer_id FROM pagos.recurring_payments WHERE id=$1',SUB)=='233'
        for mutate,restore in [
            ("UPDATE public.users SET phone='changed' WHERE id=233", "UPDATE public.users SET phone='+18095550000_duplicado_233' WHERE id=233"),
            (f"UPDATE pagos.payments SET amount=50001 WHERE id='{PAY}'",f"UPDATE pagos.payments SET amount=50000 WHERE id='{PAY}'"),
            (f"UPDATE pagos.payments SET status='PENDING' WHERE id='{PAY}'",f"UPDATE pagos.payments SET status='APPROVED' WHERE id='{PAY}'"),
            ("UPDATE public.users SET email='source@example.invalid' WHERE id=444","UPDATE public.users SET email='other@example.invalid' WHERE id=444"),
        ]:
            await conn.execute(mutate);await rejected(apply);await untouched();await conn.execute(restore)
        checks.append('changed identity/payment evidence and third-account collision roll back entirely')
        async with sessions.begin() as db:
            db.add(BillingAttemptModel(id='in-flight',subscription_id=SUB,payment_id=PAY,status='UNCERTAIN'))
        await rejected(apply);await untouched();await conn.execute("DELETE FROM pagos.billing_attempts WHERE id='in-flight'")
        async with sessions.begin() as db:
            db.add(SubscriptionActivationJobModel(payment_id=PAY,customer_id='233',status='PENDING'))
        await rejected(apply);await untouched();await conn.execute('DELETE FROM pagos.subscription_activation_jobs')
        checks.append('uncertain billing and pending activation prevent the link')
        async with sessions.begin() as db:
            db.add(RecurringPaymentModel(id='unreviewed-active',customer_id='133',amount=50000,status='ACTIVE'))
        await rejected(apply);await untouched();await conn.execute("DELETE FROM pagos.recurring_payments WHERE id='unreviewed-active'")
        checks.append('second active subscription is rejected rather than cancelled automatically')
        marker='INSERT INTO pagos.customer_identity_links(source_user_id,atlas_user_id,evidence_ref,verified_by)'
        tampered=apply.replace(marker,f"UPDATE pagos.saved_cards SET token='tampered';\n"+marker,1)
        await rejected(tampered);await untouched()
        assert await conn.fetchval('SELECT token FROM pagos.saved_cards')=='fake-existing-token'
        checks.append('protected-row fingerprint detects token mutation and rolls back')
        # Snapshot all existing business rows, including account FK dependants.
        tables=['public.users','public.reservas','pagos.payments','pagos.recurring_payments','pagos.saved_cards']
        async def snapshot():
            return {t:await conn.fetchval(f'SELECT md5(string_agg(to_jsonb(x)::text,\'|\' ORDER BY to_jsonb(x)::text)) FROM {t} x') for t in tables}
        before=await snapshot()
        await conn.execute(apply);await conn.execute(apply)
        result=json.loads(await conn.fetchval(apply.rsplit('\nCOMMIT;\n',1)[1]))
        assert result['resultado']=='APLICADO_Y_VERIFICADO' and result['atlas_user_id']==133
        assert before==await snapshot()
        checks.append('application is idempotent and preserves users, reservations, payment, subscription, dates and tokens')
        assert await conn.fetchval('SELECT count(*) FROM pagos.customer_identity_links')==1
        for uid in ['133','233','primary@example.invalid','source@example.invalid',
                    'ab95ff7a-0e9d-46c0-b553-90be40d2f655','a63e51d7-bfe0-45df-84aa-4f069c62d7b8']:
            async with sessions() as db:
                svc=RecurringService(MagicMock(),SQLRecurringRepository(db),MagicMock(),MagicMock(),db_session=db)
                with patch('app.services.recurring_service.datetime',SimpleNamespace(now=lambda tz=None:NOW)):
                    bot=await svc.get_customer_status(uid)
                web=await tokens.get_user_payment_status(uid,db)
                assert bot['allow_access'] is web['summary']['allow_access'] is True
                assert bot['reason']=='paid' and bot['valid_until']==UNTIL.isoformat()
                assert bot['active_count']==1 and not bot['requires_review']
                assert web['user_info']['customer_id']=='133'
        checks.append('real service resolves both IDs/emails/UUIDs to the same paid period without charging')
        # Raw SQL and Python must acquire exactly the same advisory lock.
        async with sessions() as db:
            await lock_customer_subscriptions(db,await resolve_customer_identity(db,'233'))
            got=await conn.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended('atlas-subscription:133',0))")
            assert not got
        checks.append('Python and SQL share the primary billing advisory lock')
        for uid in ['133','233','primary@example.invalid','source@example.invalid']:
            await rejected(f"BEGIN; INSERT INTO pagos.recurring_payments SELECT (jsonb_populate_record(NULL::pagos.recurring_payments,to_jsonb(r)||jsonb_build_object('id','duplicate-attempt','customer_id','{uid}'))).* FROM pagos.recurring_payments r WHERE id='{SUB}'; COMMIT;")
        checks.append('direct SQL cannot create a second active subscription using either identity')
        await rejected("BEGIN; UPDATE pagos.customer_identity_links SET atlas_user_id=444 WHERE source_user_id=233; COMMIT;")
        await rejected("BEGIN; INSERT INTO pagos.customer_identity_links(source_user_id,atlas_user_id,evidence_ref,verified_by) VALUES(133,233,'fake','test'); COMMIT;")
        await rejected("BEGIN; INSERT INTO pagos.customer_identity_aliases(alias,atlas_user_id,evidence_ref,verified_by) VALUES('233',133,'fake','test'); COMMIT;")
        checks.append('immutable ownership, no chains/cycles, and historical-live-ID protection remain enforced')
        async def register(email):
            async with sessions() as db:
                return await registration.register_trial(registration.RegistrationRequest(email=email,name='Fixture',last_name='Person'),db)
        results=await asyncio.gather(register('primary@example.invalid'),register('source@example.invalid'))
        assert all(r.status=='already_active' and r.customer_id=='133' for r in results)
        assert await conn.fetchval("SELECT count(*) FROM pagos.recurring_payments WHERE status='ACTIVE'")==1
        assert await conn.fetchval('SELECT next_charge_at FROM pagos.recurring_payments WHERE id=$1',SUB)==UNTIL
        assert await conn.fetchval('SELECT count(*) FROM pagos.payments')==1
        checks.append('concurrent registration reuses one membership; original payment and expiry remain intact')
        report={'postgresql':await conn.fetchval('SHOW server_version'),'environment':'disposable loopback database',
                'production_contacted':False,'real_payments_made':False,'assertions':checks,'application_result':result}
        output=Path(os.environ.get('TEST_BILLING_LINK_REPORT',str(ROOT/'billing_link_validation.json')))
        output.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))
    finally:
        if engine:await engine.dispose()
        if conn:await conn.close()
        if admin:
            if created:await admin.execute(f'DROP DATABASE "{DATABASE}"')
            await admin.close()

if __name__=='__main__':asyncio.run(main())
