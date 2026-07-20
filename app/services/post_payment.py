"""
Post-payment actions — centralised logic that runs after a checkout payment is APPROVED.

Called from all checkout exit points (direct result, 3DS-continue, 3DS term callback)
to guarantee consistent behaviour regardless of how the payment flow completed.

Actions
-------
1. Verify whether the card was saved (data_vault_token present).
2. Send a checkout payment receipt email to the cardholder.
3. Call the auth service to trigger account confirmation email
   (only effective for unconfirmed users with an active premium subscription).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import (
    Payment,
    PaymentStatus,
    RecurringPayment,
    SubscriptionStatus,
)
from app.services.notification_service import send_notification

logger = logging.getLogger(__name__)

_AUTH_API_BASE = os.getenv("AUTH_API_BASE_URL", "https://api.iamatlas.do")

# Días de gracia para usuarios nuevos
TRIAL_DAYS = 7
SUBSCRIPTION_FREQUENCY_DAYS = 30


@dataclass
class PostPaymentResult:
    """Summary of all post-payment actions taken."""
    email_sent: bool = False
    card_saved: bool = False
    confirmation_triggered: bool = False
    subscription_created: bool = False
    in_trial: bool = False
    trial_ends_at: str = ""
    cardholder_email: str = ""
    payment_id: str = ""


async def _trigger_confirmation_email(email: str) -> bool:
    """Call the auth service to send the account confirmation email.

    POST /auth/send_confirmation_email  {"email": "..."}

    The auth service checks internally whether:
    - The user exists and is not yet confirmed
    - The user has an active premium subscription (not "Gratis")

    If both conditions are met, it sends the confirmation email via SendGrid.
    Otherwise it returns a neutral 200 (no user enumeration) or 403 (no premium).

    This endpoint does NOT require auth headers — it's a public /auth route.

    Returns True if the call succeeded (200), False otherwise.
    Never raises.
    """
    url = f"{_AUTH_API_BASE}/auth/send_confirmation_email"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"email": email},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            logger.warning(
                "[post-payment] ✓ auth confirmation email triggered | email=%s status=%d",
                email, resp.status_code,
            )
            return True
        elif resp.status_code == 403:
            # 403 = subscription not active yet (possible payment processing delay)
            logger.warning(
                "[post-payment] ⚠ auth confirmation 403 — subscription not active yet | email=%s",
                email,
            )
            return False
        else:
            logger.warning(
                "[post-payment] ⚠ auth confirmation unexpected status | email=%s status=%d body=%s",
                email, resp.status_code, resp.text[:200],
            )
            return False
    except Exception as exc:
        logger.error(
            "[post-payment] ✗ auth confirmation call FAILED | email=%s err=%s",
            email, exc,
        )
        return False


async def handle_post_payment_actions(payment: Payment) -> PostPaymentResult:
    """Execute all post-payment actions for an APPROVED checkout payment.

    Parameters
    ----------
    payment : Payment
        The payment entity that was just approved.  Must have
        ``status == PaymentStatus.APPROVED`` — otherwise this is a no-op.

    Returns
    -------
    PostPaymentResult with a summary of what was done.

    This function **never raises**.  All errors are caught and logged so
    that a notification failure never blocks the checkout response.
    """
    result = PostPaymentResult(
        payment_id=payment.id,
        cardholder_email=payment.cardholder_email or "",
    )

    # Guard — only act on approved payments
    status = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
    if status != "APPROVED":
        logger.debug(
            "[post-payment] Skipped — payment %s is %s, not APPROVED",
            payment.id, status,
        )
        return result

    # ── 1. Identify if card was saved ─────────────────────────────────────
    result.card_saved = bool(payment.data_vault_token)
    logger.warning(
        "[post-payment] payment_id=%s email=%s card_saved=%s token=%s",
        payment.id,
        payment.cardholder_email or "(none)",
        result.card_saved,
        (payment.data_vault_token[:8] + "…") if payment.data_vault_token else "(none)",
    )

    # ── 2. Send checkout payment receipt email ────────────────────────────
    if payment.cardholder_email:
        card_last4 = (
            payment.card_number_masked[-4:]
            if payment.card_number_masked
            else "****"
        )
        total_amount = payment.amount + payment.itbis
        try:
            result.email_sent = await send_notification(
                event="checkout_payment_approved",
                to_email=payment.cardholder_email,
                context={
                    "amount_display": f"{total_amount / 100:.2f}",
                    "currency": payment.currency or "DOP",
                    "description": "Pago desde checkout",
                    "card_last4": card_last4,
                    "payment_id": payment.id,
                    "card_saved": result.card_saved,
                },
            )
            logger.warning(
                "[post-payment] ✓ receipt email sent=%s to=%s payment_id=%s",
                result.email_sent,
                payment.cardholder_email,
                payment.id,
            )
        except Exception as exc:
            logger.error(
                "[post-payment] ✗ receipt email FAILED | payment_id=%s to=%s err=%s",
                payment.id, payment.cardholder_email, exc,
            )

        # ── 3. Trigger account confirmation via auth service ──────────────
        #   The auth service will check if the user is unconfirmed + has a
        #   premium subscription.  If so, it sends the confirmation email.
        #   If not (already confirmed, or free tier), it's a harmless no-op.
        result.confirmation_triggered = await _trigger_confirmation_email(
            payment.cardholder_email,
        )
    else:
        logger.warning(
            "[post-payment] ⚠ no cardholder_email — skipping notifications | payment_id=%s",
            payment.id,
        )

    return result


async def create_subscription_if_needed(
    payment: Payment,
    customer_id: str,
    db: AsyncSession,
) -> PostPaymentResult:
    """Create a subscription after a successful checkout payment.

    - **New user** (no prior subscriptions or saved cards): creates a
      subscription with a 7-day trial.  ``next_charge_at`` is set to
      ``now + 7 days`` so the scheduler will fire the first real charge
      after the grace period.
    - **Existing user**: creates a subscription charged immediately
      (``last_charged_at = now``, ``next_charge_at = now + 30 days``).
    - If the user already has an ACTIVE subscription, this is a no-op
      to avoid duplicate subscriptions.

    This function **never raises**.  All errors are caught and logged.
    """
    result = PostPaymentResult(
        payment_id=payment.id,
        cardholder_email=payment.cardholder_email or "",
    )

    status = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
    if status != "APPROVED" or not payment.data_vault_token or not customer_id:
        return result

    try:
        from sqlalchemy import select
        from app.infrastructure.models import RecurringPaymentModel, SavedCardModel
        from app.infrastructure.repo_impl import SQLRecurringRepository

        # ── Check if user already has an ACTIVE subscription ──────────
        existing_active = await db.execute(
            select(RecurringPaymentModel).where(
                RecurringPaymentModel.customer_id == customer_id,
                RecurringPaymentModel.status == SubscriptionStatus.ACTIVE.value,
            )
        )
        if existing_active.scalar_one_or_none():
            logger.warning(
                "[post-payment] ⏭ user already has ACTIVE subscription — skipping | "
                "customer_id=%s payment_id=%s",
                customer_id, payment.id,
            )
            return result

        # ── Determine if user is new ──────────────────────────────────
        # New = no prior subscriptions (any status) AND no saved cards
        prior_subs = await db.execute(
            select(RecurringPaymentModel.id).where(
                RecurringPaymentModel.customer_id == customer_id,
            ).limit(1)
        )
        prior_cards = await db.execute(
            select(SavedCardModel.id).where(
                SavedCardModel.customer_id == customer_id,
            ).limit(1)
        )
        is_new_user = (
            prior_subs.scalar_one_or_none() is None
            and prior_cards.scalar_one_or_none() is None
        )

        now = datetime.now(timezone.utc)

        if is_new_user:
            # Trial: primer cobro real en TRIAL_DAYS días
            trial_end = now + timedelta(days=TRIAL_DAYS)
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=payment.amount,
                itbis=payment.itbis,
                frequency_days=SUBSCRIPTION_FREQUENCY_DAYS,
                description="Membresía Atlas",
                data_vault_token=payment.data_vault_token,
                card_brand=payment.card_number_masked[:4] if payment.card_number_masked else "",
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                card_expiration="",
                cardholder_email=payment.cardholder_email or "",
                next_charge_at=trial_end,
                last_charged_at=None,
                trial_ends_at=trial_end,
            )
            result.in_trial = True
            result.trial_ends_at = trial_end.isoformat()
            logger.warning(
                "[post-payment] ✓ NEW user — trial subscription created | "
                "customer_id=%s trial_ends=%s next_charge=%s",
                customer_id, trial_end.isoformat(), trial_end.isoformat(),
            )
        else:
            # Existing user: cobro inmediato, próximo en 30 días
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=payment.amount,
                itbis=payment.itbis,
                frequency_days=SUBSCRIPTION_FREQUENCY_DAYS,
                description="Membresía Atlas",
                data_vault_token=payment.data_vault_token,
                card_brand=payment.card_number_masked[:4] if payment.card_number_masked else "",
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                card_expiration="",
                cardholder_email=payment.cardholder_email or "",
                next_charge_at=now + timedelta(days=SUBSCRIPTION_FREQUENCY_DAYS),
                last_charged_at=now,
                trial_ends_at=None,
            )
            result.in_trial = False
            logger.warning(
                "[post-payment] ✓ EXISTING user — subscription created (no trial) | "
                "customer_id=%s next_charge=%s",
                customer_id,
                (now + timedelta(days=SUBSCRIPTION_FREQUENCY_DAYS)).isoformat(),
            )

        repo = SQLRecurringRepository(db)
        await repo.save(recurring)
        result.subscription_created = True

    except Exception as exc:
        logger.error(
            "[post-payment] ✗ subscription creation FAILED | "
            "customer_id=%s payment_id=%s err=%s",
            customer_id, payment.id, exc,
        )

    return result
