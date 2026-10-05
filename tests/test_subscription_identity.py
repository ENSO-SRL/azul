"""Regression tests use real ORM storage, with no Azul/auth/production calls."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.elements import TextClause

from app.domain.entities import Payment, PaymentStatus, SavedCard
from app.infrastructure.database import get_db
from app.infrastructure.models import PaymentModel, RecurringPaymentModel, SavedCardModel, SubscriptionActivationJobModel, BillingAttemptModel
from app.services.post_payment import create_subscription_if_needed, create_trial_subscription
from app.services.subscription_identity import resolve_customer_identity
from app.utils.token_utils import require_user_info
from routers import checkout, registration, tokens

EMAIL = "account@example.com"
UUID = "d9e1c417-43ea-407d-9ad1-d0e6f58b5b82"


def normalize_dates(target):
    # SQLite omits timezone information; PostgreSQL returns aware timestamps.
    if isinstance(target, RecurringPaymentModel):
        for field in ("trial_ends_at", "next_charge_at", "last_charged_at", "created_at", "method_updated_at"):
            value = getattr(target, field)
            if value and value.tzinfo is None:
                setattr(target, field, value.replace(tzinfo=timezone.utc))


def refreshed(target, context, attrs):
    normalize_dates(target)


class TestDB:
    """Async facade over isolated SQLite; emulate PostgreSQL advisory locks.

    Business queries and commits use real SQLAlchemy sessions. Only the
    PostgreSQL identity SELECT casts and advisory lock are translated.
    """
    __test__ = False

    def __init__(self, engine, locks):
        self.session = Session(engine, expire_on_commit=False)
        event.listen(self.session, "loaded_as_persistent", lambda session, target: normalize_dates(target))
        self.locks = locks
        self.held_lock = None

    async def execute(self, statement, params=None):
        await asyncio.sleep(0)
        if isinstance(statement, TextClause):
            sql = str(statement)
            if "pg_advisory_xact_lock" in sql:
                lock = self.locks.setdefault(params["lock_id"], asyncio.Lock())
                await lock.acquire()
                self.held_lock = lock
                return None
            if "public.users" in sql:
                statement = text(re.sub(r"\b(\w+)::text", r"CAST(\1 AS TEXT)", sql))
        return self.session.execute(statement, params or {})

    async def get(self, model, key):
        return self.session.get(model, key)

    def add(self, model):
        self.session.add(model)

    def _unlock(self):
        if self.held_lock:
            self.held_lock.release()
            self.held_lock = None

    async def commit(self):
        self.session.commit()
        self._unlock()

    async def rollback(self):
        self.session.rollback()
        self._unlock()


@pytest.fixture
def storage():
    event.listen(RecurringPaymentModel, "refresh", refreshed)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS public"))
        conn.execute(text("ATTACH DATABASE ':memory:' AS pagos"))
        conn.execute(text("CREATE TABLE public.users (id INTEGER PRIMARY KEY, email TEXT, uuid TEXT, name TEXT, last_name TEXT)"))
        conn.execute(text("INSERT INTO public.users (id, email, uuid) VALUES (228, :email, :uuid)"), {"email": EMAIL, "uuid": UUID})
        # Create the PostgreSQL model with the same partial uniqueness rule.
        conn.execute(CreateTable(RecurringPaymentModel.__table__))
        conn.execute(text("CREATE UNIQUE INDEX pagos.uq_active_sub_per_customer ON recurring_payments(customer_id) WHERE status = 'ACTIVE'"))
        SavedCardModel.__table__.create(conn)
        conn.execute(CreateTable(PaymentModel.__table__))
        SubscriptionActivationJobModel.__table__.create(conn)
        BillingAttemptModel.__table__.create(conn)
    sessions = []
    locks = {}

    def make_db():
        db = TestDB(engine, locks)
        sessions.append(db)
        return db

    yield make_db
    for db in sessions:
        db._unlock()
        db.session.close()
    engine.dispose()
    event.remove(RecurringPaymentModel, "refresh", refreshed)


def add_subscription(db, customer_id=EMAIL, *, token="", days=20, status="ACTIVE"):
    deadline = datetime.now(timezone.utc) + timedelta(days=days)
    sub = RecurringPaymentModel(
        id=str(uuid.uuid4()),
        customer_id=customer_id, amount=50000, itbis=9000, frequency_days=30,
        status=status, data_vault_token=token, trial_ends_at=deadline,
        next_charge_at=deadline,
    )
    db.session.add(sub)
    db.session.commit()
    return sub


def rows(db):
    return db.session.execute(select(RecurringPaymentModel)).scalars().all()


def card():
    return SavedCard(customer_id="228", token="vault-test", card_brand="Visa", card_last4="4242", expiration="203012")


def payment(*, order_id="CHK-TEST", email="cardholder@example.com"):
    return Payment(
        customer_id="228", amount=50000, itbis=9000, status=PaymentStatus.APPROVED,
        order_id=order_id, data_vault_token="vault-test", card_number_masked="4260********4242",
        cardholder_email=email,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["228", UUID, EMAIL, " ACCOUNT@EXAMPLE.COM "])
async def test_identity_resolves_id_uuid_and_normalized_email(storage, identifier):
    identity = await resolve_customer_identity(storage(), identifier)
    assert identity.customer_id == "228"
    assert identity.email == EMAIL
    assert set(identity.aliases) == {"228", UUID, EMAIL}


@pytest.mark.asyncio
async def test_registration_stores_id_and_is_idempotent(storage):
    db = storage()
    body = registration.RegistrationRequest(email=EMAIL, name="Test", last_name="User")
    first = await registration.register_trial(body, db)
    second = await registration.register_trial(body, db)
    assert first.status == "created"
    assert second.status == "already_active"
    assert first.customer_id == second.customer_id == "228"
    assert first.trial_ends_at == second.trial_ends_at
    assert len(rows(db)) == 1
    assert rows(db)[0].cardholder_email == EMAIL


@pytest.mark.asyncio
async def test_registration_reuses_legacy_subscription(storage):
    db = storage()
    sub = add_subscription(db)
    original_id, deadline = sub.id, sub.trial_ends_at
    body = registration.RegistrationRequest(email=EMAIL, name="Test", last_name="User")
    response = await registration.register_trial(body, db)
    assert response.status == "already_active"
    assert [(row.id, row.customer_id) for row in rows(db)] == [(original_id, "228")]
    assert response.trial_ends_at == deadline.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["228", EMAIL, UUID])
async def test_status_lookup_by_id_email_and_uuid_finds_canonical_subscription(storage, identifier):
    db = storage()
    add_subscription(db, "228")
    response = await tokens.get_user_payment_status(identifier, db)
    assert response["subscriptions_count"] == 1
    assert response["summary"]["active_subscriptions"] == 1
    assert response["user_info"]["email"] == EMAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("customer_id", [EMAIL, "228", UUID])
@pytest.mark.parametrize("flow", ["tokenize", "sale", "hold"])
async def test_checkout_reuses_existing_trial_and_attaches_card(storage, customer_id, flow):
    db = storage()
    sub = add_subscription(db, customer_id)
    original_id, deadline, next_charge = sub.id, sub.trial_ends_at, sub.next_charge_at
    with patch("app.services.post_payment._trigger_confirmation_email", new=AsyncMock(return_value=True)):
        if flow == "tokenize":
            result = await create_trial_subscription("228", card(), 100, 0, "cardholder@example.com", db)
        else:
            result = await create_subscription_if_needed(
                payment(order_id="HOLD-TEST" if flow == "hold" else "CHK-TEST"), "228", db, card_expiration="203012",
            )
    saved = rows(db)
    assert len(saved) == 1
    assert saved[0].id == original_id
    assert saved[0].customer_id == "228"
    assert saved[0].data_vault_token == "vault-test"
    assert saved[0].card_expiration == "203012"
    assert saved[0].trial_ends_at == deadline
    if flow == "sale":
        assert saved[0].last_charged_at is not None
        assert saved[0].next_charge_at == saved[0].last_charged_at + timedelta(days=30)
    else:
        assert saved[0].next_charge_at == next_charge
        assert saved[0].last_charged_at is None
    assert not result.subscription_created
    assert result.subscription_updated and result.in_trial
    assert result.trial_ends_at == deadline.isoformat()


@pytest.mark.asyncio
async def test_repeated_checkout_keeps_trial_date_and_existing_card(storage):
    db = storage()
    sub = add_subscription(db, "228", token="original-vault")
    deadline = sub.trial_ends_at
    result = await create_trial_subscription(
        "228", card(), 50000, 9000, EMAIL, db,
        promo_code="ATLAS2026UP", user_name="alejandro bobadilla",
    )
    assert not result.subscription_created
    assert result.subscription_updated
    assert len(rows(db)) == 1
    assert rows(db)[0].trial_ends_at == deadline
    assert rows(db)[0].data_vault_token == "vault-test"


@pytest.mark.asyncio
async def test_expired_cardless_trial_real_sale_advances_paid_cycle(storage):
    db = storage()
    sub = add_subscription(db, days=-2)
    deadline = sub.trial_ends_at
    result = await create_subscription_if_needed(payment(), "228", db, card_expiration="203012")
    assert not result.in_trial
    assert len(rows(db)) == 1
    assert rows(db)[0].trial_ends_at == deadline
    assert rows(db)[0].last_charged_at is not None
    assert rows(db)[0].next_charge_at == rows(db)[0].last_charged_at + timedelta(days=30)


@pytest.mark.asyncio
async def test_cardholder_email_does_not_link_another_account(storage):
    db = storage()
    db.session.execute(text("INSERT INTO public.users (id, email, uuid) VALUES (999, 'other@example.com', 'other-uuid')"))
    db.session.commit()
    other = add_subscription(db, "other@example.com")
    result = await create_subscription_if_needed(payment(email="other@example.com"), "228", db)
    assert result.subscription_created
    assert len(rows(db)) == 2
    assert other.customer_id == "other@example.com"
    assert not other.data_vault_token
    assert {row.customer_id for row in rows(db)} == {"228", "other@example.com"}


@pytest.mark.asyncio
async def test_existing_duplicates_block_creation(storage):
    db = storage()
    add_subscription(db, EMAIL)
    add_subscription(db, "228", token="old-vault")
    result = await create_subscription_if_needed(payment(), "228", db)
    assert result.subscription_error
    assert not result.subscription_created
    assert len(rows(db)) == 2


@pytest.mark.asyncio
async def test_missing_account_does_not_create_subscription(storage):
    db = storage()
    result = await create_subscription_if_needed(payment(), "missing-id", db)
    assert result.subscription_error
    assert not rows(db)


@pytest.mark.asyncio
async def test_identity_database_failure_rolls_back_without_creating():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("lookup unavailable")
    result = await create_subscription_if_needed(payment(), "228", db)
    db.rollback.assert_awaited_once()
    assert result.subscription_error
    assert not result.subscription_created


@pytest.mark.asyncio
async def test_concurrent_registration_and_checkout_share_one_subscription(storage):
    registration_db, checkout_db = storage(), storage()
    body = registration.RegistrationRequest(email=EMAIL, name="Test", last_name="User")
    with patch("app.services.post_payment._trigger_confirmation_email", new=AsyncMock(return_value=True)):
        await asyncio.gather(
            registration.register_trial(body, registration_db),
            create_trial_subscription("228", card(), 50000, 9000, EMAIL, checkout_db),
        )
    saved = rows(storage())
    assert len(saved) == 1
    assert saved[0].customer_id == "228"
    assert saved[0].data_vault_token == "vault-test"


def checkout_app(db, token_svc, payment_svc):
    app = FastAPI()
    app.include_router(checkout.router, dependencies=[Depends(require_user_info)])
    app.dependency_overrides[require_user_info] = lambda: {"sub": "228", "email": EMAIL}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[checkout._get_token_svc] = lambda: token_svc
    app.dependency_overrides[checkout._get_service] = lambda: payment_svc
    return app


async def checkout_request(db, token_svc, payment_svc):
    app = checkout_app(db, token_svc, payment_svc)
    with patch("app.utils.token_utils.decode_user_info_token", return_value={"sub": "228", "email": EMAIL}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies={"checkout_csrf": "csrf"}) as client:
            return await client.post("/checkout/process", data={
                "card_number": "4260550061845872", "cardholder_name": "Test User",
                "expiration": "12/30", "cvc": "123", "cardholder_email": "cardholder@example.com",
                "csrf_token": "csrf",
            })


@pytest.mark.asyncio
async def test_checkout_http_preserves_legacy_trial_without_sale(storage):
    db = storage()
    sub = add_subscription(db)
    deadline = sub.trial_ends_at
    token_svc, payment_svc = AsyncMock(), AsyncMock()
    token_svc.register_card.return_value = card()
    with patch("app.services.post_payment._trigger_confirmation_email", new=AsyncMock(return_value=True)):
        response = await checkout_request(db, token_svc, payment_svc)
    assert response.status_code == 200
    token_svc.register_card.assert_awaited_once()
    assert token_svc.register_card.call_args.kwargs["customer_id"] == "228"
    payment_svc.process_sale.assert_not_awaited()
    assert len(rows(db)) == 1
    assert rows(db)[0].trial_ends_at == deadline


@pytest.mark.asyncio
async def test_checkout_does_not_charge_trial_if_tokenization_is_unavailable(storage):
    db = storage()
    add_subscription(db)
    token_svc, payment_svc = AsyncMock(), AsyncMock()
    token_svc.register_card.side_effect = ValueError("VALIDATION_ERROR:TrxType")
    from app.infrastructure.azul_gateway import AzulIntegrationError
    payment_svc.process_hold_verify.side_effect = AzulIntegrationError("VALIDATION_ERROR:TrxType")
    response = await checkout_request(db, token_svc, payment_svc)
    assert response.status_code == 503
    payment_svc.process_sale.assert_not_awaited()
    assert len(rows(db)) == 1
    assert not rows(db)[0].data_vault_token


@pytest.mark.asyncio
async def test_checkout_identity_failure_blocks_gateway():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("lookup unavailable")
    token_svc, payment_svc = AsyncMock(), AsyncMock()
    response = await checkout_request(db, token_svc, payment_svc)
    assert response.status_code == 503
    token_svc.register_card.assert_not_awaited()
    payment_svc.process_sale.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_existing_duplicates_block_gateway(storage):
    db = storage()
    add_subscription(db)
    add_subscription(db, "228")
    token_svc, payment_svc = AsyncMock(), AsyncMock()
    response = await checkout_request(db, token_svc, payment_svc)
    assert response.status_code == 409
    token_svc.register_card.assert_not_awaited()
    payment_svc.process_sale.assert_not_awaited()
