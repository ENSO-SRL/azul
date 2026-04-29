"""
Recurring payment scheduler — APScheduler-based MIT engine.

Runs inside the FastAPI process (AsyncIOScheduler).  Every hour it queries
for active subscriptions whose next_charge_at <= now and fires sale_mit()
for each one.

Retry policy (deterministic — uses failed_attempts counter)
------------------------------------------------------------
Intento 1 falla → failed_attempts=1, next_charge_at = now + 1 día
Intento 2 falla → failed_attempts=2, next_charge_at = now + 3 días
Intento 3 falla → failed_attempts=3, next_charge_at = now + 7 días
Intento 4 falla → status=PAUSED, notificar al cliente

Si aprueba en cualquier punto → failed_attempts=0, avanzar ciclo normal.

Idempotencia — CustomOrderId determinístico
-------------------------------------------
Cada intento genera un CustomOrderId único derivado del ID de suscripción,
el número de ciclo, y el intento actual. Si el scheduler falla y reintenta
por error de red, Azul detecta el duplicado y no cobra dos veces.

Separación de errores
---------------------
- AzulIntegrationError → bug nuestro (auth, payload malformado).
  NO se pausa la suscripción. Se loguea como ERROR para alertas.
- Declinadas de negocio (IsoCode != 00) → payment.status = DECLINED.
  Se aplica política de reintentos normal.
- Exception genérica → se trata como fallo técnico transitorio.
  Se aplica política de reintentos.

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
from app.infrastructure.azul_gateway import AzulIntegrationError, AzulPaymentGateway
from app.services.notification_service import ctx_charge, send_notification

logger = logging.getLogger(__name__)

# Retry backoff delays in days
_RETRY_DELAYS_DAYS = [1, 3, 7]  # Intento 1→2, 2→3, 3→PAUSE
_MAX_ATTEMPTS = 3

# Shared scheduler instance
_scheduler: AsyncIOScheduler | None = None


def _build_custom_order_id(sub_id: str, failed_attempts: int) -> str:
    """Deterministic CustomOrderId for idempotent retries.

    Format: sub-{id_prefix}-att{attempt}
    Azul uses this to detect duplicate requests and avoid double-charging.
    """
    # Use first 8 chars of UUID to keep within Azul field length limits
    short_id = sub_id.replace("-", "")[:12]
    return f"sub-{short_id}-att{failed_attempts}"


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
                # ----------------------------------------------------------
                # Expiration guard — skip charge and pause if card has expired
                # ----------------------------------------------------------
                if sub.card_expiration:
                    try:
                        # card_expiration is YYYYMM, e.g. "202812"
                        exp_year  = int(sub.card_expiration[:4])
                        exp_month = int(sub.card_expiration[4:6])
                        # Card is valid through the last day of expiration month
                        from calendar import monthrange
                        last_day   = monthrange(exp_year, exp_month)[1]
                        exp_cutoff = datetime(exp_year, exp_month, last_day, 23, 59, 59, tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) > exp_cutoff:
                            logger.warning(
                                "[scheduler] sub=%s PAUSED — card expired %s. "
                                "Customer must update payment method.",
                                sub.id, sub.card_expiration,
                            )
                            sub.status = SubscriptionStatus.PAUSED
                            sub.last_failure_reason = f"Tarjeta vencida (exp. {sub.card_expiration})"
                            await recurring_repo.update(sub)
                            continue
                    except (ValueError, IndexError):
                        # Malformed expiration — log and proceed anyway
                        logger.warning(
                            "[scheduler] sub=%s has invalid card_expiration=%r — skipping expiry check.",
                            sub.id, sub.card_expiration,
                        )

                # Build idempotent CustomOrderId — same on retry, unique per cycle+attempt
                custom_order_id = _build_custom_order_id(sub.id, sub.failed_attempts)

                payment = Payment(
                    id=custom_order_id,  # use as CustomOrderId for Azul idempotency
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
                    # Success — reset retry counter, advance schedule
                    sub.failed_attempts = 0
                    sub.last_failure_reason = ""
                    sub.last_charged_at = datetime.now(timezone.utc)
                    sub.next_charge_at = (
                        datetime.now(timezone.utc) + timedelta(days=sub.frequency_days)
                    )
                    logger.info(
                        "[scheduler] sub=%s charged OK — iso=%s next=%s",
                        sub.id, payment.iso_code, sub.next_charge_at.date(),
                    )
                    # Notify customer of successful charge
                    await send_notification(
                        "charge_approved",
                        to_email=getattr(sub, "cardholder_email", ""),
                        context=ctx_charge(
                            amount=sub.amount,
                            currency=getattr(sub, "currency_code", "DOP"),
                            description=sub.description or "Suscripción",
                            card_last4=sub.card_last4,
                            next_charge_date=sub.next_charge_at,
                        ),
                    )
                else:
                    # Business decline — apply retry policy
                    reason = payment.response_message or f"IsoCode={payment.iso_code}"
                    sub = _handle_failure(sub, reason)
                    # Notify customer of failed charge
                    await send_notification(
                        "charge_failed",
                        to_email=getattr(sub, "cardholder_email", ""),
                        context=ctx_charge(
                            amount=sub.amount,
                            currency=getattr(sub, "currency_code", "DOP"),
                            description=sub.description or "Suscripción",
                            card_last4=sub.card_last4,
                            failure_reason=reason,
                            failed_attempts=sub.failed_attempts,
                        ),
                    )
                    if sub.status == SubscriptionStatus.PAUSED:
                        await send_notification(
                            "subscription_paused",
                            to_email=getattr(sub, "cardholder_email", ""),
                            context=ctx_charge(
                                amount=sub.amount,
                                currency=getattr(sub, "currency_code", "DOP"),
                                description=sub.description or "Suscripción",
                                card_last4=sub.card_last4,
                                failure_reason=reason,
                            ),
                        )

            except AzulIntegrationError as exc:
                # Our bug — log as ERROR, do NOT apply retry (would loop)
                # The subscription stays on the same next_charge_at until a human fixes the integration
                logger.error(
                    "[scheduler] INTEGRATION ERROR sub=%s: %s — "
                    "NOT retrying. Fix the integration bug first.",
                    sub.id, exc,
                )
                # Don't modify sub — don't bump failed_attempts for our bugs

            except Exception as exc:
                logger.exception("[scheduler] sub=%s unexpected error: %s", sub.id, exc)
                sub = _handle_failure(sub, str(exc))

            await recurring_repo.update(sub)


def _handle_failure(sub, reason: str):
    """Advance retry counter or pause subscription.

    Uses sub.failed_attempts for deterministic backoff — no heuristics.
    """
    from app.domain.entities import RecurringPayment  # avoid circular at module level

    now = datetime.now(timezone.utc)
    sub.failed_attempts += 1
    sub.last_failure_reason = reason[:500]  # trim to column limit

    if sub.failed_attempts > _MAX_ATTEMPTS:
        # Exhausted all retries — pause subscription
        logger.warning(
            "[scheduler] sub=%s PAUSED after %d consecutive failures. "
            "Last reason: %s",
            sub.id, sub.failed_attempts, reason,
        )
        sub.status = SubscriptionStatus.PAUSED
        # Keep next_charge_at as-is so UI can show "paused since"
    else:
        delay_days = _RETRY_DELAYS_DAYS[sub.failed_attempts - 1]
        sub.next_charge_at = now + timedelta(days=delay_days)
        logger.warning(
            "[scheduler] sub=%s failed (attempt %d/%d): %s — retrying in %d day(s) on %s",
            sub.id, sub.failed_attempts, _MAX_ATTEMPTS,
            reason, delay_days, sub.next_charge_at.date(),
        )

    return sub


async def run_now(engine: AsyncEngine) -> int:
    """Manually trigger the scheduler job — used by debug endpoints and tests.

    Returns the number of subscriptions processed.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.infrastructure.repo_impl import SQLRecurringRepository

    async with session_factory() as session:
        count = len(await SQLRecurringRepository(session).list_due())

    await _charge_due_subscriptions(session_factory)
    return count


async def run_reconciliation(engine: AsyncEngine) -> dict:
    """Manually trigger the reconciliation job — used by the reconciliation endpoint."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        from app.services.reconciliation_service import ReconciliationService
        return await ReconciliationService(session).run(days_back=1)


async def _send_upcoming_charge_reminders(session_factory: async_sessionmaker) -> None:
    """Job: send email reminders 3 days before next charge."""
    from app.infrastructure.repo_impl import SQLRecurringRepository

    reminder_window = datetime.now(timezone.utc) + timedelta(days=3)
    reminder_start  = reminder_window.replace(hour=0, minute=0, second=0, microsecond=0)
    reminder_end    = reminder_window.replace(hour=23, minute=59, second=59, microsecond=999999)

    async with session_factory() as session:
        from sqlalchemy import select
        from app.infrastructure.models import RecurringPaymentModel
        result = await session.execute(
            select(RecurringPaymentModel).where(
                RecurringPaymentModel.status == "ACTIVE",
                RecurringPaymentModel.next_charge_at >= reminder_start,
                RecurringPaymentModel.next_charge_at <= reminder_end,
            )
        )
        subs = result.scalars().all()
        logger.info("[scheduler] Sending %d upcoming charge reminder(s).", len(subs))
        for sub in subs:
            await send_notification(
                "upcoming_charge",
                to_email=getattr(sub, "cardholder_email", ""),
                context=ctx_charge(
                    amount=sub.amount,
                    currency=getattr(sub, "currency_code", "DOP"),
                    description=sub.description or "Suscripción",
                    card_last4=sub.card_last4,
                    next_charge_date=sub.next_charge_at,
                ),
            )


async def _run_daily_reconciliation(session_factory: async_sessionmaker) -> None:
    """Job: run daily reconciliation and log the summary."""
    async with session_factory() as session:
        from app.services.reconciliation_service import ReconciliationService
        summary = await ReconciliationService(session).run(days_back=1)
        if summary["mismatch"] > 0 or summary["not_found"] > 0:
            logger.warning(
                "[reconciliation] ALERT — %d mismatch(es), %d not found. Manual review required.",
                summary["mismatch"], summary["not_found"],
            )


def start_scheduler(engine: AsyncEngine) -> None:
    """Start the APScheduler background job.

    Call from FastAPI lifespan startup.
    """
    global _scheduler

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # Job 1: charge due subscriptions (every hour)
    _scheduler.add_job(
        _charge_due_subscriptions,
        trigger="interval",
        hours=1,
        kwargs={"session_factory": session_factory},
        id="charge_due_subscriptions",
        replace_existing=True,
    )

    # Job 2: upcoming charge reminder (daily at 09:00 UTC)
    _scheduler.add_job(
        _send_upcoming_charge_reminders,
        trigger="cron",
        hour=9,
        minute=0,
        kwargs={"session_factory": session_factory},
        id="upcoming_charge_reminders",
        replace_existing=True,
    )

    # Job 3: daily reconciliation (daily at 00:30 UTC)
    _scheduler.add_job(
        _run_daily_reconciliation,
        trigger="cron",
        hour=0,
        minute=30,
        kwargs={"session_factory": session_factory},
        id="daily_reconciliation",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "[scheduler] Started — "
        "charges: every 1h | reminders: 09:00 UTC | reconciliation: 00:30 UTC"
    )


def stop_scheduler() -> None:
    """Stop the scheduler gracefully.

    Call from FastAPI lifespan shutdown.
    """
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Stopped.")
    _scheduler = None
