"""Shared pytest fixtures / test bootstrap.

The scheduler performs lazy (function-local) imports of its repositories and
gateway, so ``app.infrastructure.repo_impl`` is not imported at module load.
``unittest.mock.patch("app.infrastructure.repo_impl.X")`` resolves its target
string via ``importlib`` + ``getattr`` and fails if the submodule was never
imported. Import the modules eagerly here so patch targets always resolve.
"""

from __future__ import annotations

import app.infrastructure.repo_impl  # noqa: F401
import app.infrastructure.azul_gateway  # noqa: F401


# Older service tests isolate repositories entirely. Supply explicit identity and
# ledger doubles there; durable uniqueness has separate tests against real SQL.
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from app.services import recurring_service as rs
from app.domain.entities import RecurringPayment

@pytest.fixture(autouse=True)
def legacy_service_dependencies(request, monkeypatch):
    if request.module.__name__.split(".")[-1] not in ("test_recurring", "test_scheduler"):
        return
    original = rs.RecurringService.__init__
    async def identity(db, customer):
        return SimpleNamespace(customer_id=customer, aliases=(customer,), email="")
    async def active(db, identity):
        candidate = await db.test_repo.get_by_id("ignored")
        return candidate if isinstance(candidate, RecurringPayment) else None
    def init(service, *args, **kwargs):
        original(service, *args, **kwargs)
        if service._db is None:
            service._db = MagicMock()
        service._db.test_repo = service._recurring
        service._db.test_payments = service._payments
        service._db.execute = AsyncMock()
        service._db.get = AsyncMock(return_value=None)
        service._db.commit = AsyncMock()
        service._db.rollback = AsyncMock()
    monkeypatch.setattr(rs.RecurringService, "__init__", init)
    monkeypatch.setattr(rs, "resolve_customer_identity", identity)
    monkeypatch.setattr(rs, "find_active_subscription", active)
    monkeypatch.setattr(rs, "lock_customer_subscriptions", AsyncMock())
    from app.services import subscription_identity
    monkeypatch.setattr(subscription_identity, "resolve_customer_identity", identity)
    monkeypatch.setattr(subscription_identity, "lock_customer_subscriptions", AsyncMock())
    monkeypatch.setattr(rs, "BillingAttempts", lambda db: SimpleNamespace(reserve=AsyncMock(), finish=AsyncMock(), uncertain=AsyncMock()))
    # These legacy unit tests isolate orchestration; real lifecycle/SQL behavior
    # is exercised in test_payment_lifecycle and the PostgreSQL integration suite.
    from app.services import payment_lifecycle as lifecycle
    async def begin(db,payment,**kwargs): return payment,True
    async def complete(db,payment): await db.test_payments.save(payment)
    monkeypatch.setattr(lifecycle,'begin_payment',begin)
    monkeypatch.setattr(lifecycle,'complete_payment',complete)
    monkeypatch.setattr(lifecycle,'mark_uncertain',AsyncMock())
    monkeypatch.setattr(lifecycle,'assert_no_pending_membership',AsyncMock())
    monkeypatch.setattr(lifecycle,'subscription_history',AsyncMock(return_value=[]))
    if request.module.__name__.split(".")[-1] == "test_scheduler":
        from app.services import scheduler
        async def notify(*args, **kwargs):
            return await scheduler.send_notification(*args, **kwargs)
        monkeypatch.setattr(rs, "send_notification", notify)
        from app.infrastructure import repo_impl
        original_charge = rs.RecurringService.charge
        async def charge(service, recurring_id):
            due = service._recurring.list_due.return_value
            service._recurring.get_by_id.return_value = next(s for s in due if s.id == recurring_id)
            return await original_charge(service, recurring_id)
        monkeypatch.setattr(rs.RecurringService, "charge", charge)
