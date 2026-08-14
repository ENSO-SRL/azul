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
from app.services.user_service_client import enviar_correo_pago

logger = logging.getLogger(__name__)

# Retry backoff delays in days
_RETRY_DELAYS_DAYS = [1, 3, 7]  # Intento 1→2, 2→3, 3→PAUSE
_MAX_ATTEMPTS = 3

# Shared scheduler instance
_scheduler: AsyncIOScheduler | None = None


def build_custom_order_id(sub_id: str, failed_attempts: int, cycle: str) -> str:
    """Deterministic CustomOrderId for idempotent retries.

    Format: sub-{id_prefix}-c{cycle}-att{attempt}
    Azul uses this to detect duplicate requests and avoid double-charging.
    """
    # Use first 8 chars of UUID to keep within Azul field length limits
    short_id = sub_id.replace("-", "")[:12]
    return f"sub-{short_id}-c{cycle}-att{failed_attempts}"


def _currency_str(sub) -> str:
    """Return the subscription currency as a plain ISO string (e.g. 'DOP')."""
    c = getattr(sub, "currency_code", "DOP")
    return c.value if hasattr(c, "value") else str(c or "DOP")


async def verify_prior_charge(gateway: AzulPaymentGateway, custom_order_id: str) -> dict | None:
    """Best-effort check: does Azul already have an APPROVED tx for this order?

    Closes the residual double-charge window where the process crashed AFTER
    Azul charged the card but BEFORE the local payment row was saved (so the
    DB idempotency latch cannot see it).

    Returns the Azul response dict when the transaction is found + approved,
    else None. NEVER raises — on any verify error it returns None so a verify
    outage does not block billing (the DB latch still guards the common case).
    """
    try:
        data = await gateway.verify_payment(custom_order_id)
    except Exception as e:  # network, integration error, unparseable — do not block
        logger.warning(
            "[idempotency] verify_payment(%s) failed: %s — proceeding to charge.",
            custom_order_id, e,
        )
        return None
    found = data.get("Found") in (True, "true", "True", 1)
    iso = str(data.get("IsoCode", ""))
    if found and iso == "00":
        return data
    return None


async def notify_charge_outcome(sub, payment, invoice_number: str) -> None:
    """Send the customer email for a subscription charge result.

    Single source of truth for charge notifications, used by the scheduler,
    the manual charge endpoint, and the first charge at subscription creation
    so all three behave identically:

    - APPROVED            → billing receipt / invoice (via the user service).
    - DECLINED + PAUSED   → a single 'subscription_paused' email (SES). The
      per-attempt failure email is intentionally skipped here so a pause does
      NOT send two emails for the same event.
    - DECLINED (retrying) → 'charge failed' billing email (via the user service).

    Never raises — the underlying senders swallow their own errors.
    """
    payment_method = f"Tarjeta terminada en {sub.card_last4}" if sub.card_last4 else None

    if payment.status == PaymentStatus.APPROVED:
        await enviar_correo_pago(
            to_email=sub.cardholder_email,
            success=True,
            invoice_number=invoice_number,
            total=sub.amount / 100,
            currency=_currency_str(sub),
            payment_method=payment_method,
        )
        return

    reason = payment.response_message or f"IsoCode={payment.iso_code}"

    if sub.status == SubscriptionStatus.PAUSED:
        # Retries exhausted — one dedicated "paused" email, not the failure one.
        await send_notification(
            "subscription_paused",
            to_email=sub.cardholder_email,
            context=ctx_charge(
                amount=sub.amount,
                currency=_currency_str(sub),
                description=sub.description or "Suscripción",
                card_last4=sub.card_last4,
                failure_reason=reason,
            ),
        )
    else:
        await enviar_correo_pago(
            to_email=sub.cardholder_email,
            success=False,
            currency=_currency_str(sub),
            payment_method=payment_method,
            failure_reason=reason,
        )


async def _charge_due_subscriptions(session_factory: async_sessionmaker) -> None:
    """Job body: charge all subscriptions that are due.

    Uses a Redis distributed lock to prevent double-charging when
    multiple ECS tasks run simultaneously (e.g. during rolling deploys).
    """
    # ── Distributed lock ─────────────────────────────────────────────────
    from app.infrastructure.redis_client import get_redis
    redis = get_redis()
    lock_key = "atlas:scheduler:charge_lock"
    lock_ttl = 600  # 10 minutes — enough for the job to complete
    acquired = False
    if redis:
        try:
            acquired = await redis.set(lock_key, "1", nx=True, ex=lock_ttl)
            if not acquired:
                logger.info("[scheduler] Another instance holds the charge lock — skipping this run.")
                return
        except Exception as e:
            # FAIL-CLOSED: si el lock no se puede evaluar no cobramos. Perder una
            # corrida horaria es preferible a un doble cobro cuando hay varias
            # instancias ECS activas (ej. durante un rolling deploy) y Redis caído.
            # La siguiente corrida reintentará cuando Redis se recupere.
            logger.error(
                "[scheduler] Redis lock unavailable (%s) — SKIPPING this run to avoid "
                "concurrent double-charging. Will retry next cycle.", e,
            )
            return
    else:
        acquired = True  # no Redis configured, proceed normally (single instance)

    try:
        await _charge_due_subscriptions_inner(session_factory)
    finally:
        if redis and acquired:
            try:
                await redis.delete(lock_key)
            except Exception:
                pass  # lock will auto-expire via TTL


async def _charge_due_subscriptions_inner(session_factory: async_sessionmaker) -> None:
    """Inner charging logic — called under the distributed lock."""
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
                            # Tell the customer to update their card (was dead code before).
                            await send_notification(
                                "card_expired",
                                to_email=sub.cardholder_email,
                                context=ctx_charge(
                                    amount=sub.amount,
                                    currency=_currency_str(sub),
                                    description=sub.description or "Suscripción",
                                    card_last4=sub.card_last4,
                                ),
                            )
                            continue
                    except (ValueError, IndexError):
                        # Malformed expiration — log and proceed anyway
                        logger.warning(
                            "[scheduler] sub=%s has invalid card_expiration=%r — skipping expiry check.",
                            sub.id, sub.card_expiration,
                        )

                # Build idempotent CustomOrderId — same on retry, unique per billing
                # date + attempt. IMPORTANT: the cycle key uses the full date
                # (YYYYMMDD), not the calendar month, so two 30-day charges that fall
                # in the same month (e.g. Jan 1 + Jan 31) do NOT collide on the same id.
                cycle = (
                    sub.next_charge_at.strftime("%Y%m%d")
                    if sub.next_charge_at
                    else datetime.now(timezone.utc).strftime("%Y%m%d")
                )
                custom_order_id = build_custom_order_id(sub.id, sub.failed_attempts, cycle)

                # ── Idempotency guard (two layers) ───────────────────────────
                # 1) DB latch: a local payment row for this deterministic id is
                #    already APPROVED (crash AFTER charging + saving, but before
                #    advancing next_charge_at).
                # 2) Azul verify: the card was charged at Azul but the local row
                #    was never saved (crash BETWEEN charging and saving).
                # In either case do NOT charge again — advance the schedule.
                existing = await payment_repo.get_by_id(custom_order_id)
                db_ok = existing is not None and existing.status == PaymentStatus.APPROVED

                azul_prior = None
                if not db_ok:
                    azul_prior = await verify_prior_charge(gateway, custom_order_id)

                if db_ok or azul_prior is not None:
                    logger.warning(
                        "[scheduler] sub=%s already charged this cycle (order=%s, "
                        "source=%s) — advancing schedule WITHOUT re-charging.",
                        sub.id, custom_order_id, "db" if db_ok else "azul",
                    )
                    # If Azul charged but we have no local record, persist an audit
                    # row so history/reconciliation stay consistent.
                    if existing is None and azul_prior is not None:
                        recovered = Payment(
                            id=custom_order_id,
                            amount=sub.amount,
                            itbis=sub.itbis,
                            payment_type=PaymentType.RECURRING,
                            order_id=f"sub-{sub.id}",
                            auth_mode="splitit",
                            initiated_by="merchant",
                            currency_code=sub.currency_code,
                        )
                        recovered.status = PaymentStatus.APPROVED
                        recovered.iso_code = "00"
                        recovered.azul_order_id = (
                            azul_prior.get("AzulOrderId") or azul_prior.get("AZULOrderId", "")
                        )
                        recovered.authorization_code = azul_prior.get("AuthorizationCode", "")
                        recovered.rrn = azul_prior.get("RRN", "")
                        recovered.response_message = "Recuperado vía verify_payment (idempotencia)"
                        await payment_repo.save(recovered)

                    sub.failed_attempts = 0
                    sub.last_failure_reason = ""
                    sub.last_charged_at = (
                        existing.created_at if existing is not None else datetime.now(timezone.utc)
                    )
                    sub.next_charge_at = datetime.now(timezone.utc) + timedelta(days=sub.frequency_days)
                    await recurring_repo.update(sub)
                    continue

                payment = Payment(
                    id=custom_order_id,  # use as CustomOrderId for Azul idempotency
                    amount=sub.amount,
                    itbis=sub.itbis,
                    payment_type=PaymentType.RECURRING,
                    order_id=f"sub-{sub.id}",
                    auth_mode="splitit",
                    initiated_by="merchant",
                    currency_code=sub.currency_code,
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
                else:
                    # Business decline — apply retry policy (may PAUSE the sub)
                    reason = payment.response_message or f"IsoCode={payment.iso_code}"
                    sub = _handle_failure(sub, reason)

                # Single notification path — one email per outcome (no duplicates).
                await notify_charge_outcome(sub, payment, custom_order_id)

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
                to_email=sub.cardholder_email or "",
                context=ctx_charge(
                    amount=sub.amount,
                    currency=_currency_str(sub),
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


async def _purge_old_transactions(session_factory: async_sessionmaker) -> None:
    """Job: eliminar transacciones DECLINED/VOID antiguas (política de retención).

    Configuración:
        DATA_RETENTION_DAYS — días a retener (default 90).
        Solo borra transacciones con status DECLINED o sin azul_order_id (fallidas).
        Los pagos APPROVED se conservan indefinidamente para auditoría.
    """
    import os as _os
    from sqlalchemy import delete, text
    from app.infrastructure.models import PaymentModel, TransactionModel

    retention_days = int(_os.getenv("DATA_RETENTION_DAYS", "90"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    async with session_factory() as session:
        # 1. Delete transactions linked to old DECLINED payments
        from sqlalchemy import select
        declined_ids = select(PaymentModel.id).where(
            PaymentModel.status == "DECLINED",
            PaymentModel.created_at < cutoff,
        )
        del_txns = await session.execute(
            delete(TransactionModel).where(
                TransactionModel.payment_id.in_(declined_ids)
            )
        )
        # 2. Delete the old DECLINED payment records
        del_pmts = await session.execute(
            delete(PaymentModel).where(
                PaymentModel.status == "DECLINED",
                PaymentModel.created_at < cutoff,
            )
        )
        await session.commit()
        logger.info(
            "[retention] Purged %d transaction(s) and %d payment(s) older than %d days.",
            del_txns.rowcount, del_pmts.rowcount, retention_days,
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

    # Job 4: data retention — purge old DECLINED records (daily at 02:00 UTC)
    _scheduler.add_job(
        _purge_old_transactions,
        trigger="cron",
        hour=2,
        minute=0,
        kwargs={"session_factory": session_factory},
        id="data_retention",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "[scheduler] Started — "
        "charges: every 1h | reminders: 09:00 UTC | "
        "reconciliation: 00:30 UTC | retention: 02:00 UTC"
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
