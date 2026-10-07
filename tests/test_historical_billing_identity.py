"""Historical IDs need explicit evidence; cardholder email never grants access."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from test_subscription_identity import storage, add_subscription, EMAIL, UUID

from app.infrastructure.models import PaymentModel
from app.infrastructure.repo_impl import SQLRecurringRepository
from app.services.recurring_service import RecurringService
from app.services.subscription_identity import CustomerIdentityError, resolve_customer_identity
from routers import recurring, tokens


def link(db, old="172", current=228):
    db.session.execute(text("INSERT INTO pagos.customer_identity_aliases VALUES (:alias,:user,'approved-payment-and-reconciliation','operator-review')"),
                       {"alias": old, "user": current})
    db.session.commit()


def service(db):
    gateway = MagicMock(sale_mit=AsyncMock(), create_token=AsyncMock(), verify_payment=AsyncMock())
    return RecurringService(MagicMock(), SQLRecurringRepository(db), MagicMock(), gateway, db_session=db), gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["228", "172", UUID, EMAIL])
async def test_reviewed_historical_id_resolves_both_directions(storage, identifier):
    db = storage(); link(db)
    identity = await resolve_customer_identity(db, identifier)
    assert identity.customer_id == "228"
    assert set(identity.aliases) == {"228", "172", UUID, EMAIL}


@pytest.mark.asyncio
async def test_roberto_case_expired_email_subscription_cannot_hide_reviewed_paid_period(storage):
    db = storage()
    add_subscription(db, EMAIL, token="old-token", days=-1)
    paid = add_subscription(db, "172", token="paid-token", days=-40)
    charged = datetime.now(timezone.utc) - timedelta(days=3)
    paid.last_charged_at = charged
    paid.next_charge_at = charged + timedelta(days=30)
    paid.cardholder_email = EMAIL
    db.session.commit()
    svc, gateway = service(db)
    before = await svc.get_customer_status("228")
    assert not before["allow_access"] and before["reason"] == "payment_due"
    assert before["total_subscriptions"] == 1
    link(db)
    for identifier in ("228", "172", EMAIL, UUID):
        after = await svc.get_customer_status(identifier)
        assert after["allow_access"] and not after["needs_payment"]
        assert after["reason"] == "paid"
        assert after["valid_until"] == (charged + timedelta(days=30)).isoformat()
        assert after["requires_review"] and after["total_subscriptions"] == 2
    assert paid.customer_id == "172"  # Reads preserve historical transaction identity.
    gateway.sale_mit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreviewed_orphan_and_matching_cardholder_email_do_not_grant_access(storage):
    db = storage(); paid = add_subscription(db, "172", token="paid", days=-40)
    paid.cardholder_email = EMAIL
    paid.last_charged_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.session.commit()
    svc, _ = service(db)
    assert not (await svc.get_customer_status("228"))["allow_access"]
    with pytest.raises(CustomerIdentityError):
        await resolve_customer_identity(db, "172")


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["228", "172", EMAIL])
async def test_reused_historical_id_fails_closed_for_all_entry_points(storage, identifier):
    db = storage(); link(db)
    db.session.execute(text("INSERT INTO public.users (id,email,uuid) VALUES (172,'other@example.com','other-uuid')"))
    db.session.commit()
    with pytest.raises(CustomerIdentityError):
        await resolve_customer_identity(db, identifier)


@pytest.mark.asyncio
async def test_duplicate_alias_subscriptions_block_charge_before_gateway(storage):
    db = storage(); link(db)
    old = add_subscription(db, EMAIL, token="old-token", days=-40)
    old.card_expiration = "209912"
    add_subscription(db, "172", token="paid-token", days=-40)
    db.session.commit()
    svc, gateway = service(db)
    with pytest.raises(CustomerIdentityError):
        await svc.charge(old.id)
    gateway.sale_mit.assert_not_awaited()
    gateway.verify_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_summary_and_bot_share_aliases_and_entitlement(storage):
    db = storage(); link(db)
    paid = add_subscription(db, "172", token="paid-token", days=-40)
    paid.last_charged_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.add(PaymentModel(id="historical-payment", customer_id="172", amount=50000, itbis=9000,
                        payment_type="RECURRING", status="APPROVED"))
    db.session.commit()
    web = await tokens.get_user_payment_status("228", db)
    bot = await recurring.get_customer_status("228", service(db)[0])
    assert web["user_info"]["customer_id"] == "228"
    assert web["subscriptions_count"] == bot["total_subscriptions"] == 1
    assert web["summary"]["allow_access"] == bot["allow_access"] is True
    assert web["summary"]["valid_until"] == bot["valid_until"]
    assert any(p["id"] == "historical-payment" for p in web["recent_payments"])


@pytest.mark.asyncio
async def test_identity_error_is_not_reported_as_unpaid(storage):
    db = storage()
    for call in (tokens.get_user_payment_status("missing", db), recurring.get_customer_status("missing", service(db)[0])):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_missing_mapping_table_is_an_operational_error(storage):
    db = storage()
    db.session.execute(text("DROP TABLE pagos.customer_identity_aliases")); db.session.commit()
    with pytest.raises(Exception, match="customer_identity_aliases"):
        await service(db)[0].get_customer_status("228")
