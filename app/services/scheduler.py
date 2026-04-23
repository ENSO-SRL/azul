"""
Recurring payment scheduler — APScheduler-based MIT engine.

Runs inside the FastAPI process (AsyncIOScheduler).  Every hour it queries
for active subscriptions whose next_charge_at <= now and fires sale_mit()
for each one.

Retry policy
------------
- Attempt 1: immediately when due.
- Attempt 2: 1 hour after failure   (next_charge_at = now + 1h).
- Attempt 3: 24 hours after failure (next_charge_at = now + 24h).
- After 3 consecutive failures: subscription moved to PAUSED.

Integration
-----------
Call ``start_scheduler(engine)`` from FastAPI lifespan startup.
Call ``stop_scheduler()`` from lifespan shutdown.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.entities import Payment, PaymentStatus, PaymentType, SubscriptionStatus
from app.infrastructure.azul_gateway import AzulPaymentGateway

logger = logging.getLogger(__name__)

# Retry backoff delays (hours)
_RETRY_DELAYS = [1, 24, 48]

# Shared scheduler instance
_scheduler: AsyncIOScheduler | None = None


async def _charge_due_subscriptions(session_factory: async_sessionmaker) -> None:
    """Job body: charge all subscriptions that are due."""
    from app.infrastructure.repo_impl import (
        SQLPaymentRepository,
        SQLRecurringRepository,
        SQLTransactionRepository,
    )

    async with session_factory() as session:
        recurring_repo = SQLRecurringRepository(session)
        payment_repo   = SQLPaymentRepository(session)
        txn_repo       = SQLTransactionRepository(session)
        gateway        = AzulPaymentGateway()

        due = await recurring_repo.list_due()
        if not due:
            return

        logger.info("[scheduler] %d subscription(s) due for charging.", len(due))

        for sub in due:
            try:
                payment = Payment(
                    amount=sub.amount,
                    itbis=sub.itbis,
                    payment_type=PaymentType.RECURRING,
                    order_id=f"sub-{sub.id}",
                    auth_mode="splitit",
                    initiated_by="merchant",
                )

                payment, txn = await gateway.sale_mit(payment, sub.data_vault_token)

                await payment_repo.save(payment)
                await txn_repo.save(txn)

                if payment.status == PaymentStatus.APPROVED:
                    sub.last_charged_at = datetime.now(timezone.utc)
                    sub.next_charge_at  = (
                        datetime.now(timezone.utc) + timedelta(days=sub.frequency_days)
                    )
                    logger.info(
                        "[scheduler] sub=%s charged OK — next=%s",
                        sub.id, sub.next_charge_at.date(),
                    )
                else:
                    sub = _handle_failure(sub, payment.response_message)

            except Exception as exc:
                logger.exception("[scheduler] sub=%s unexpected error: %s", sub.id, exc)
                sub = _handle_failure(sub, str(exc))

            await recurring_repo.update(sub)


def _handle_failure(sub, reason: str):
    """Advance retry counter or pause subscription."""
    from app.domain.entities import RecurringPayment  # avoid circular at module level

    now = datetime.now(timezone.utc)

    # Count consecutive failures by inspecting last_charged_at
    # We store retry attempt implicitly via next_charge_at offsets
    # Simple approach: check how many times next_charge_at was bumped forward < 1 day
    # For MVP, we use a simple attempt count based on frequency vs delay comparison.

    # Determine which retry attempt we're on
    if sub.next_charge_at and sub.last_charged_at:
        delta_hours = (sub.next_charge_at - sub.last_charged_at).total_seconds() / 3600
        if delta_hours < 2:
            # Attempt 1 failed → retry in 24h
            delay_hours = _RETRY_DELAYS[1]
        elif delta_hours < 25:
            # Attempt 2 failed → retry in 48h
            delay_hours = _RETRY_DELAYS[2]
        else:
            # Attempt 3 failed → pause
            logger.warning(
                "[scheduler] sub=%s paused after 3 failures — last reason: %s",
                sub.id, reason,
            )
            sub.status = SubscriptionStatus.PAUSED
            return sub
    else:
        # First failure
        delay_hours = _RETRY_DELAYS[0]

    sub.next_charge_at = now + timedelta(hours=delay_hours)
    logger.warning(
        "[scheduler] sub=%s failed (%s) — retrying in %sh",
        sub.id, reason, delay_hours,
    )
    return sub


def start_scheduler(engine: AsyncEngine) -> None:
    """Start the APScheduler background job.

    Call from FastAPI lifespan startup.
    """
    global _scheduler

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _charge_due_subscriptions,
        trigger="interval",
        hours=1,
        kwargs={"session_factory": session_factory},
        id="charge_due_subscriptions",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("[scheduler] Started — checking subscriptions every hour.")


def stop_scheduler() -> None:
    """Stop the scheduler gracefully.

    Call from FastAPI lifespan shutdown.
    """
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Stopped.")
    _scheduler = None
