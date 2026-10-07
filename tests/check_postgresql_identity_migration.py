"""Integration check against a disposable loopback PostgreSQL database ONLY.

Set TEST_LOCAL_PG_PORT to the port of the dedicated test cluster. This script
never accepts a remote hostname or production database name.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = int(os.environ["TEST_LOCAL_PG_PORT"])
DATABASE = os.environ["TEST_LOCAL_PG_DATABASE"]
assert DATABASE.startswith("codex_identity_test_") and DATABASE.replace("_", "").isalnum()
DSN = f"postgresql://codex_test@127.0.0.1:{PORT}/{DATABASE}"
os.environ["DATABASE_URL"] = DSN.replace("postgresql://", "postgresql+asyncpg://")
os.environ["DB_SSL"] = "disable"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

from app.infrastructure.models import RecurringPaymentModel, PaymentModel, ReconciliationReportModel, BillingAttemptModel
from app.infrastructure.repo_impl import SQLRecurringRepository
from app.services.recurring_service import RecurringService

PAID_ID = "aa2a676c-966b-491b-bb4c-75396559496e"
PAYMENT_ID = "sub-aa2a676c966b-c20261004-att0"
DUPLICATE_ID = "b2fbfdd2-ddae-4ffb-ac79-d7eaea65b3c5"
NOW = datetime(2026, 10, 7, 21, tzinfo=timezone.utc)


async def main():
    conn = await asyncpg.connect(DSN, timeout=5)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    session = async_sessionmaker(engine, expire_on_commit=False)
    assertions = []
    try:
        # Refuse to reuse a non-empty database, even on loopback.
        assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema IN ('public','pagos'))")
        await conn.execute("""CREATE SCHEMA pagos;
            CREATE TABLE public.users (id integer PRIMARY KEY, email text UNIQUE, uuid text,
              name text,last_name text,phone text,is_active boolean,is_confirmed boolean,parent_id integer);
            INSERT INTO public.users VALUES
              (259,'fixture@example.com','fixture-uuid','Roberto','Borda','18090000044',true,true,null),
              (237,'other@example.com','other-uuid','Other','Person','18090004726',true,true,null);""")
        for model in (RecurringPaymentModel, PaymentModel, ReconciliationReportModel, BillingAttemptModel):
            await conn.execute(str(CreateTable(model.__table__).compile(dialect=postgresql.dialect())))
        await conn.execute("""INSERT INTO pagos.recurring_payments
            (id,customer_id,amount,itbis,frequency_days,description,status,data_vault_token,card_brand,card_last4,card_expiration,
             next_charge_at,last_charged_at,failed_attempts,last_failure_reason,created_at,cardholder_email,trial_ends_at,currency_code)
            VALUES ($1,'172',50000,9000,30,'Fixture paid membership','ACTIVE','fake-token','Visa','0000','209912',
                    '2026-11-03 23:34:35.413172+00','2026-10-04 23:34:35.413169+00',0,'',now(),'fixture@example.com',null,'DOP'),
                   ($2,'fixture@example.com',200,36,30,'Fixture expired membership','ACTIVE','old-fake-token','Visa','0000','209912',
                    '2026-10-07 15:26:28.886635+00','2026-09-07 15:26:28.886629+00',0,'',now(),'fixture@example.com',null,'DOP')""", PAID_ID, DUPLICATE_ID)
        # Legacy FK column exists in the live schema but is not mapped by the ORM.
        await conn.execute("ALTER TABLE pagos.payments ADD COLUMN atlas_user_id integer")
        async with session.begin() as db:
            db.add(PaymentModel(id=PAYMENT_ID,customer_id="172",order_id="REC-16D77E9D",
                amount=50000,itbis=9000,currency="DOP",payment_type="RECURRING",status="APPROVED",iso_code="00"))
        await conn.execute("""INSERT INTO pagos.reconciliation_reports
            (id,payment_id,run_date,custom_order_id,local_status,local_iso_code,azul_status,azul_iso_code,azul_order_id,status,notes,checked_at)
            VALUES ('fixture-reconciliation',$1,'2026-10-05','REC-16D77E9D','APPROVED','00','FOUND','00','','OK','',now())""", PAYMENT_ID)
        schema = (ROOT / "migrations/20261007_customer_identity_aliases.sql").read_text(encoding="utf-8")
        reviewed = (ROOT / "operations/roberto_172_to_259_review.sql").read_text(encoding="utf-8")
        await conn.execute(schema)
        await conn.execute(schema)
        assertions.append("schema migration is idempotent on PostgreSQL")

        async def status(identifier):
            async with session() as db:
                svc = RecurringService(MagicMock(), SQLRecurringRepository(db), MagicMock(), MagicMock(), db_session=db)
                with patch("app.services.recurring_service.datetime", SimpleNamespace(now=lambda tz=None: NOW)):
                    return await svc.get_customer_status(identifier)

        before = await status("259")
        assert before["reason"] == "payment_due" and not before["allow_access"]
        await conn.execute(reviewed)
        assert await conn.fetchval("SELECT count(*) FROM pagos.customer_identity_aliases") == 0
        assertions.append("review script rolls back by default")
        approved = reviewed.replace("\nROLLBACK;", "\nCOMMIT;")
        await conn.execute(approved)
        await conn.execute(approved)
        assert await conn.fetchval("SELECT count(*) FROM pagos.customer_identity_aliases") == 1
        assertions.append("reviewed association is idempotent")
        after = await status("259")
        for identifier in ("259", "172", "fixture@example.com", "fixture-uuid"):
            found = await status(identifier)
            assert found["allow_access"] and found["reason"] == "paid" and found["requires_review"]
            assert found["total_subscriptions"] == 2
            assert found["valid_until"] == "2026-11-03T23:34:35.413169+00:00"
        assertions.append("actual service finds paid period through every verified identity")
        assert await conn.fetchval("SELECT customer_id FROM pagos.recurring_payments WHERE id=$1", PAID_ID) == "172"
        assert await conn.fetchval("SELECT atlas_user_id FROM pagos.payments WHERE id=$1", PAYMENT_ID) is None
        assertions.append("historical payments and subscription records stay unchanged")

        async def rejected(sql):
            try:
                async with conn.transaction():
                    await conn.execute(sql)
            except (asyncpg.RaiseError, asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError):
                return
            raise AssertionError("Unsafe mutation accepted: " + sql)

        await rejected("UPDATE pagos.customer_identity_aliases SET atlas_user_id=237 WHERE alias='172'")
        await rejected("INSERT INTO pagos.customer_identity_aliases(alias,atlas_user_id,evidence_ref,verified_by) VALUES ('237',259,'test','test')")
        await rejected("INSERT INTO pagos.customer_identity_aliases(alias,atlas_user_id,evidence_ref,verified_by) VALUES ('173',999999,'test','test')")
        await rejected("INSERT INTO pagos.customer_identity_aliases(alias,atlas_user_id,evidence_ref,verified_by) VALUES ('cardholder@example.com',259,'test','test')")
        await rejected(f"UPDATE pagos.recurring_payments SET customer_id='259' WHERE id='{DUPLICATE_ID}'")
        assertions.append("database rejects reassignment, live-identity collision, missing owner, email heuristic and duplicate active writes")
        await conn.execute("UPDATE public.users SET phone='changed' WHERE id=259")
        try:
            await conn.execute(approved)
        except asyncpg.RaiseError:
            await conn.execute("ROLLBACK")
        else:
            raise AssertionError("Evidence drift must abort the association")
        assertions.append("review script aborts when evidence changed")
        await conn.execute("UPDATE public.users SET phone='18090000044' WHERE id=259")
        pause_review = (ROOT / "operations/roberto_pause_expired_duplicate_review.sql").read_text(encoding="utf-8")
        # Freeze only this disposable test's SQL clock, preserving production guards.
        pause_review = pause_review.replace("now()", "'2026-10-07 21:00:00+00'::timestamptz")
        await conn.execute(pause_review)
        assert await conn.fetchval("SELECT status FROM pagos.recurring_payments WHERE id=$1", DUPLICATE_ID) == "ACTIVE"
        pause_approved = pause_review.replace("\nROLLBACK;", "\nCOMMIT;")
        await conn.execute(pause_approved)
        await conn.execute(pause_approved)
        after = await status("259")
        assert after["allow_access"] and not after["requires_review"] and after["active_count"] == 1
        assert after["valid_until"] == "2026-11-03T23:34:35.413169+00:00"
        assert await conn.fetchval("SELECT data_vault_token FROM pagos.recurring_payments WHERE id=$1", DUPLICATE_ID) == "old-fake-token"
        assertions.append("reviewed duplicate pause is idempotent, preserves paid access and card tokens, and leaves one active subscription")
        result = {"assertions": assertions, "before": before, "after": after, "environment": "disposable local PostgreSQL 17"}
        report = Path(os.environ["TEST_LOCAL_PG_REPORT"])
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"assertions": assertions, "before_access": before["allow_access"],
                          "after_access": after["allow_access"], "valid_until": after["valid_until"]}, indent=2))
    finally:
        await engine.dispose()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
