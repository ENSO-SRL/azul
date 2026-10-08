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
import sqlalchemy.exc
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import (
    Payment,
    Currency,
    PaymentStatus,
    RecurringPayment,
    SubscriptionStatus,
)
from app.services.notification_service import send_notification
from app.services.subscription_identity import (
    find_active_subscription,
    lock_customer_subscriptions,
    resolve_customer_identity,
)

logger = logging.getLogger(__name__)

def _detect_brand_from_masked(masked: str) -> str:
    """Detect card brand from masked card number or DataVault token prefix.

    Visa BINs start with 4, Mastercard with 5 or 2.
    If the masked number has visible BIN digits, use them.
    Otherwise return empty string.
    """
    if not masked:
        return ""
    # Strip asterisks and spaces to find any visible digits
    digits = masked.lstrip("*").lstrip(" ")
    first_digit = ""
    for ch in masked:
        if ch.isdigit():
            first_digit = ch
            break
    if first_digit == "4":
        return "Visa"
    elif first_digit in ("5", "2"):
        return "Mastercard"
    return ""

_AUTH_API_BASE = os.getenv("AUTH_API_BASE_URL", "https://api.iamatlas.do")

# Días de gracia para usuarios nuevos
TRIAL_DAYS = 7
PROMO_CODE_30_DAYS = "ATLAS2026UP"
PROMO_CODE_TRIAL_DAYS = 60
# Nombres autorizados para usar el código ATLAS2026UP (normalizados a minúsculas sin tildes)
PROMO_ALLOWED_NAMES = {
    "alejandro bobadilla",         # 1
    "daniel paulino",              # 2
    "juan jose nunez",             # 5
    "marco macarrulla",            # 6
    "manuel perello",              # 7
    "danilo bobadilla",            # 8
    "fernando ramos villanueva",   # 10
    "luis zadkiel duran aracena",  # Zadkiel Duran
}
SUBSCRIPTION_FREQUENCY_DAYS = 30


def _normalize_name(name: str) -> str:
    """Normaliza un nombre: minúsculas, sin tildes, sin espacios extra."""
    import unicodedata
    name = name.strip().lower()
    # Remover tildes/acentos
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    # Colapsar espacios múltiples
    return " ".join(name.split())


def _is_promo_allowed(user_name: str) -> bool:
    """Verifica si el nombre del usuario está en la lista de permitidos para el promo."""
    if not user_name:
        return False
    normalized = _normalize_name(user_name)
    # Verificar si el nombre normalizado contiene o es contenido por algún nombre permitido
    for allowed in PROMO_ALLOWED_NAMES:
        if allowed in normalized or normalized in allowed:
            return True
    return False

# Monto real de la membresía (centavos) — para el cobro recurrente.
# El Hold de verificación usa RD$1.00 pero la suscripción debe cobrar el precio real.
MEMBERSHIP_AMOUNT = int(os.getenv("MEMBERSHIP_AMOUNT", "50000"))   # RD$500.00
MEMBERSHIP_ITBIS  = int(os.getenv("MEMBERSHIP_ITBIS", "9000"))     # RD$90.00


@dataclass
class PostPaymentResult:
    """Summary of all post-payment actions taken."""
    email_sent: bool = False
    card_saved: bool = False
    confirmation_triggered: bool = False
    subscription_created: bool = False
    subscription_updated: bool = False
    subscription_error: str = ""
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
        total_amount = payment.amount
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


async def _reuse_active_subscription(
    db, identity, existing_sub, result, *, token: str, card_brand: str,
    card_last4: str, card_expiration: str, cardholder_email: str,
    charged: bool = False, charged_at=None,
) -> None:
    """Attach a first card to the existing record without granting another trial."""
    now = datetime.now(timezone.utc)
    changed = existing_sub.customer_id != identity.customer_id
    existing_sub.customer_id = identity.customer_id
    method_date = charged_at or now
    previous_method_date = getattr(existing_sub, "method_updated_at", None)
    if token and (not previous_method_date or method_date >= previous_method_date):
        existing_sub.method_updated_at = method_date
        existing_sub.data_vault_token = token
        existing_sub.card_brand = card_brand
        existing_sub.card_last4 = card_last4
        existing_sub.card_expiration = card_expiration
        existing_sub.cardholder_email = cardholder_email or identity.email
        changed = True
    if charged:
        paid_at = charged_at or now
        if not existing_sub.last_charged_at or existing_sub.last_charged_at < paid_at:
            existing_sub.last_charged_at = paid_at
            existing_sub.next_charge_at = paid_at + timedelta(days=existing_sub.frequency_days)
            existing_sub.failed_attempts = 0
            existing_sub.last_failure_reason = ""
            changed = True
    result.subscription_updated = changed
    result.in_trial = bool(existing_sub.trial_ends_at and existing_sub.trial_ends_at > now)
    result.trial_ends_at = existing_sub.trial_ends_at.isoformat() if existing_sub.trial_ends_at else ""
    result.card_saved = bool(existing_sub.data_vault_token)
    await db.commit()


async def _activate_subscription(
    payment: Payment,
    customer_id: str,
    db: AsyncSession,
    card_expiration: str = "",
    promo_code: str | None = None,
    user_name: str = "",
) -> PostPaymentResult:
    """Create a subscription after a successful checkout payment.

    - **New user** (no prior subscriptions or saved cards): creates a
      subscription with a 7-day trial.  ``next_charge_at`` is set to
      ``now + 7 days`` so the scheduler will fire the first real charge
      after the grace period.
    - **Existing user**: creates a subscription charged immediately
      (``last_charged_at = now``, ``next_charge_at = now + 30 days``).
    - Reuses an ACTIVE subscription under the Atlas ID or a legacy email.
      A cardless trial receives the card without changing its expiry.

    This function **never raises**.  All errors are caught and logged.
    """
    result = PostPaymentResult(
        payment_id=payment.id,
        cardholder_email=payment.cardholder_email or "",
    )

    status = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
    if status != "APPROVED" or not customer_id:
        return result
    from sqlalchemy import select
    from app.infrastructure.models import BillingAttemptModel
    import json
    attempts=(await db.execute(select(BillingAttemptModel).where(BillingAttemptModel.payment_id==payment.id))).scalars().all()
    terms=next((json.loads(a.context_json or '{}') for a in attempts if json.loads(a.context_json or '{}').get('kind')=='subscription'),{})
    membership_amount=terms.get('amount',MEMBERSHIP_AMOUNT)
    membership_itbis=terms.get('itbis',MEMBERSHIP_ITBIS)
    frequency=terms.get('frequency_days',SUBSCRIPTION_FREQUENCY_DAYS)
    verification_only = (payment.order_id or "").startswith("HOLD-")
    if not verification_only and payment.amount < membership_amount:
        result.subscription_error = "El importe aprobado requiere revisión antes de activar la membresía."
        return result
    paid_at = payment.created_at


    try:
        from sqlalchemy import select
        from app.infrastructure.models import RecurringPaymentModel, SavedCardModel
        from app.infrastructure.repo_impl import SQLRecurringRepository

        identity = await resolve_customer_identity(db, customer_id)
        customer_id = identity.customer_id
        await lock_customer_subscriptions(db, identity)
        existing_sub = await find_active_subscription(db, identity, for_update=True)
        if existing_sub is None:
            # If cancellation won a race with the bank response, preserve it.
            # Record the paid period on the original subscription without renewing consent.
            target_ids=[a.subscription_id for a in attempts if json.loads(a.context_json or '{}').get('membership')]
            existing_sub=(await db.execute(select(RecurringPaymentModel).where(
                RecurringPaymentModel.id.in_(target_ids),
                identity.matches(RecurringPaymentModel.customer_id)).with_for_update())).scalar_one_or_none()
        if existing_sub:
            await _reuse_active_subscription(
                db, identity, existing_sub, result,
                token=payment.data_vault_token,
                card_brand=_detect_brand_from_masked(payment.card_number_masked),
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                card_expiration=card_expiration,
                cardholder_email=payment.cardholder_email,
                charged=not (payment.order_id or "").startswith("HOLD-"),
                charged_at=payment.created_at,
            )
            return result

        # ── Check for existing active trial ───────────────────────────
        now = datetime.now(timezone.utc)
        
        active_trial_query = await db.execute(
            select(RecurringPaymentModel.trial_ends_at).where(
                identity.matches(RecurringPaymentModel.customer_id),
                RecurringPaymentModel.trial_ends_at > now,
            ).order_by(RecurringPaymentModel.trial_ends_at.desc()).limit(1)
        )
        remaining_trial_end = active_trial_query.scalar_one_or_none()

        # ── Determine if user is completely new ───────────────────────
        # New = no prior subscriptions (any status) AND no saved cards
        prior_subs = await db.execute(
            select(RecurringPaymentModel.id).where(
                identity.matches(RecurringPaymentModel.customer_id),
            ).limit(1)
        )
        prior_cards = await db.execute(
            select(SavedCardModel.id).where(
                identity.matches(SavedCardModel.customer_id),
                SavedCardModel.token != payment.data_vault_token,
            ).limit(1)
        )
        is_new_user = (
            prior_subs.scalar_one_or_none() is None
            and prior_cards.scalar_one_or_none() is None
        )

        if remaining_trial_end and verification_only:
            # Heredar período de gracia restante
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=membership_amount,
                itbis=membership_itbis,
                frequency_days=frequency,
                description=terms.get('description') or 'Membresía Atlas',
                currency_code=Currency(terms.get('currency','DOP')),
                data_vault_token=payment.data_vault_token,
                card_brand=_detect_brand_from_masked(payment.card_number_masked),
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                card_expiration=card_expiration,
                cardholder_email=payment.cardholder_email or "",
                next_charge_at=remaining_trial_end,
                last_charged_at=None,
                trial_ends_at=remaining_trial_end,
            )
            result.in_trial = True
            result.trial_ends_at = remaining_trial_end.isoformat()
            logger.warning(
                "[post-payment] ✓ EXISTING user — inheriting remaining trial | "
                "customer_id=%s trial_ends=%s next_charge=%s",
                customer_id, remaining_trial_end.isoformat(), remaining_trial_end.isoformat(),
            )
        elif is_new_user and verification_only:
            # Trial: primer cobro real en TRIAL_DAYS días
            # Descuento especial de 30 días con código promocional o ID
            special_30_day_users = [
                # "ID_DEL_USUARIO_AQUI",
            ]
            trial_days_for_user = TRIAL_DAYS
            logger.warning("[post-payment] DEBUG PROMO NEW | promo_code=%s user_name='%s'", promo_code, user_name)
            if promo_code and promo_code.strip().upper() == PROMO_CODE_30_DAYS and _is_promo_allowed(user_name):
                # Anti-trampa: verificar si ya usó el promo en alguna suscripción anterior
                prior_promo = await db.execute(
                    select(RecurringPaymentModel.id).where(
                        identity.matches(RecurringPaymentModel.customer_id),
                        RecurringPaymentModel.trial_ends_at.isnot(None),
                    ).limit(1)
                )
                if prior_promo.scalar_one_or_none() is not None:
                    logger.warning(
                        "[post-payment] ⚠ PROMO REJECTED — user already used promo before | "
                        "customer_id=%s promo=%s",
                        customer_id, PROMO_CODE_30_DAYS,
                    )
                else:
                    trial_days_for_user = PROMO_CODE_TRIAL_DAYS
            elif customer_id in special_30_day_users:
                trial_days_for_user = PROMO_CODE_TRIAL_DAYS
                
            trial_end = now + timedelta(days=trial_days_for_user)
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=membership_amount,
                itbis=membership_itbis,
                frequency_days=frequency,
                description=terms.get('description') or 'Membresía Atlas',
                currency_code=Currency(terms.get('currency','DOP')),
                data_vault_token=payment.data_vault_token,
                card_brand=_detect_brand_from_masked(payment.card_number_masked),
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                card_expiration=card_expiration,
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
            # Existing user (sin trial): cobro inmediato, próximo en 30 días
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=membership_amount,
                itbis=membership_itbis,
                frequency_days=frequency,
                description=terms.get('description') or 'Membresía Atlas',
                currency_code=Currency(terms.get('currency','DOP')),
                data_vault_token=payment.data_vault_token,
                card_brand=_detect_brand_from_masked(payment.card_number_masked),
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                card_expiration=card_expiration,
                cardholder_email=payment.cardholder_email or "",
                next_charge_at=paid_at + timedelta(days=frequency),
                last_charged_at=paid_at,
                trial_ends_at=None,
            )
            result.in_trial = False
            logger.warning(
                "[post-payment] ✓ EXISTING user — subscription created (no trial) | "
                "customer_id=%s next_charge=%s",
                customer_id,
                (now + timedelta(days=frequency)).isoformat(),
            )

        repo = SQLRecurringRepository(db)
        await repo.save(recurring)
        result.subscription_created = True

    except sqlalchemy.exc.IntegrityError as exc:
        await db.rollback()
        result.subscription_error = "No se pudo guardar la suscripción. Intenta de nuevo."
        logger.warning(
            "[post-payment] ⚠ colisión al crear suscripción (ya existe una activa) | "
            "customer_id=%s payment_id=%s",
            customer_id, payment.id
        )
    except Exception as exc:
        await db.rollback()
        result.subscription_error = "No se pudo verificar o actualizar la suscripción. Intenta de nuevo."
        logger.error(
            "[post-payment] ✗ subscription creation FAILED | "
            "customer_id=%s payment_id=%s err=%s",
            customer_id, payment.id, exc,
        )

    return result


async def create_trial_subscription(
    customer_id: str,
    saved_card,
    amount: int,       # DEPRECATED — ignored, uses MEMBERSHIP_AMOUNT
    itbis: int,        # DEPRECATED — ignored, uses MEMBERSHIP_ITBIS
    cardholder_email: str,
    db: AsyncSession,
    promo_code: str | None = None,
    user_name: str = "",
) -> PostPaymentResult:
    """Create a trial subscription from a tokenized card (no charge).

    Called when a NEW user registers their card for the first time via
    checkout.  The card was tokenized via TrxType=CREATE (no charge).

    The subscription always uses MEMBERSHIP_AMOUNT/MEMBERSHIP_ITBIS,
    regardless of the amount/itbis parameters (which may be from a Hold).

    Creates a RecurringPayment with:
    - trial_ends_at = now + TRIAL_DAYS
    - next_charge_at = now + TRIAL_DAYS  (scheduler will charge after trial)
    - last_charged_at = None  (no charge has been made)

    This function **never raises**.  All errors are caught and logged.
    """
    result = PostPaymentResult(
        cardholder_email=cardholder_email,
    )

    try:
        from sqlalchemy import select
        from app.infrastructure.models import RecurringPaymentModel
        from app.infrastructure.repo_impl import SQLRecurringRepository

        if not customer_id or not saved_card.token:
            result.subscription_error = "Falta el usuario o el token de la tarjeta."
            return result
        identity = await resolve_customer_identity(db, customer_id)
        customer_id = identity.customer_id
        await lock_customer_subscriptions(db, identity)
        existing_sub = await find_active_subscription(db, identity, for_update=True)
        if existing_sub:
            await _reuse_active_subscription(
                db, identity, existing_sub, result,
                token=saved_card.token,
                card_brand=getattr(saved_card, "card_brand", ""),
                card_last4=saved_card.card_last4,
                card_expiration=getattr(saved_card, "expiration", ""),
                cardholder_email=cardholder_email,
            )
            if result.subscription_updated and identity.email:
                result.confirmation_triggered = await _trigger_confirmation_email(identity.email)
            return result

        now = datetime.now(timezone.utc)
        from app.services.payment_lifecycle import subscription_history, assert_no_pending_membership
        history = await subscription_history(db, identity)
        from app.services.access_policy import access_decision
        if history:
            if access_decision(history)['allow_access']:
                prior = max(history, key=lambda row: row.last_charged_at or row.created_at)
                await _reuse_active_subscription(db, identity, prior, result,
                    token=saved_card.token, card_brand=getattr(saved_card,'card_brand',''),
                    card_last4=saved_card.card_last4, card_expiration=getattr(saved_card,'expiration',''),
                    cardholder_email=cardholder_email)
                return result
            result.subscription_error = 'Esta cuenta ya tuvo una suscripción; no corresponde otra prueba gratuita.'
            await db.rollback()
            return result
        await assert_no_pending_membership(db, identity)

        # Descuento especial de 30 días con código promocional o ID
        special_30_day_users = [
            # "ID_DEL_USUARIO_AQUI",
        ]
        trial_days_for_user = TRIAL_DAYS
        logger.warning(
            "[post-payment] DEBUG PROMO INPUTS | promo_code=%s user_name='%s'",
            promo_code, user_name
        )
        if promo_code and promo_code.strip().upper() == PROMO_CODE_30_DAYS and _is_promo_allowed(user_name):
            # Anti-trampa: verificar si ya usó el promo en alguna suscripción anterior
            prior_promo = await db.execute(
                select(RecurringPaymentModel.id).where(
                    identity.matches(RecurringPaymentModel.customer_id),
                    RecurringPaymentModel.trial_ends_at.isnot(None),
                ).limit(1)
            )
            if prior_promo.scalar_one_or_none() is not None:
                logger.warning(
                    "[post-payment] ⚠ PROMO REJECTED — user already used promo before | "
                    "customer_id=%s promo=%s",
                    customer_id, PROMO_CODE_30_DAYS,
                )
            else:
                trial_days_for_user = PROMO_CODE_TRIAL_DAYS
        elif customer_id in special_30_day_users:
            trial_days_for_user = PROMO_CODE_TRIAL_DAYS
            
        trial_end = now + timedelta(days=trial_days_for_user)

        recurring = RecurringPayment(
            customer_id=customer_id,
            amount=MEMBERSHIP_AMOUNT,
            itbis=MEMBERSHIP_ITBIS,
            frequency_days=SUBSCRIPTION_FREQUENCY_DAYS,
            description="Membresía Atlas",
            data_vault_token=saved_card.token,
            card_brand=getattr(saved_card, "card_brand", ""),
            card_last4=saved_card.card_last4,
            card_expiration=getattr(saved_card, "expiration", ""),
            cardholder_email=cardholder_email,
            next_charge_at=trial_end,
            last_charged_at=None,
            trial_ends_at=trial_end,
        )

        repo = SQLRecurringRepository(db)
        await repo.save(recurring)

        result.subscription_created = True
        result.in_trial = True
        result.trial_ends_at = trial_end.isoformat()

        logger.warning(
            "[post-payment] ✓ TRIAL subscription created (tokenize-only, no charge) | "
            "customer_id=%s trial_ends=%s next_charge=%s token=%s",
            customer_id,
            trial_end.isoformat(),
            trial_end.isoformat(),
            saved_card.token[:12] + "…" if saved_card.token else "(none)",
        )

        # Trigger account confirmation email (auth service)
        if cardholder_email:
            result.confirmation_triggered = await _trigger_confirmation_email(
                cardholder_email,
            )

    except Exception as exc:
        await db.rollback()
        result.subscription_error = "No se pudo verificar o actualizar la suscripción. Intenta de nuevo."
        logger.error(
            "[post-payment] ✗ trial subscription creation FAILED | "
            "customer_id=%s err=%s",
            customer_id, exc,
        )

    return result


async def create_subscription_if_needed(payment, customer_id, db, card_expiration="", promo_code=None, user_name=""):
    """Persist activation work; retrying activation never submits another charge."""
    from sqlalchemy import select
    from app.infrastructure.models import SubscriptionActivationJobModel
    result = PostPaymentResult(payment_id=payment.id)
    if payment.status != PaymentStatus.APPROVED:
        return result
    try:
        # An approved service/club payment must not accidentally buy membership.
        import json
        from app.infrastructure.models import BillingAttemptModel
        operations=(await db.execute(select(BillingAttemptModel).where(
            BillingAttemptModel.payment_id==payment.id,
            BillingAttemptModel.request_fingerprint!=''))).scalars().all()
        if operations and not any(json.loads(a.context_json or '{}').get('membership') for a in operations):
            return result
        job = await db.get(SubscriptionActivationJobModel, payment.id)
        if job is None:
            job = SubscriptionActivationJobModel(payment_id=payment.id, customer_id=customer_id,
                card_expiration=card_expiration, promo_code=promo_code or "", user_name=user_name)
            db.add(job)
            await db.commit()
        job = (await db.execute(select(SubscriptionActivationJobModel).where(
            SubscriptionActivationJobModel.payment_id == payment.id).with_for_update())).scalar_one()
        from app.infrastructure.models import PaymentModel
        current=(await db.execute(select(PaymentModel).where(PaymentModel.id==payment.id)
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if job.status=='CANCELLED' or (current and current.status in ('VOIDED','REFUNDED')):
            job.status='CANCELLED'
            await db.commit()
            result.subscription_error='El pago fue devuelto; no corresponde activar una membresía.'
            return result
        if job.status == "DONE":
            result.subscription_updated = True
            await db.commit()
            return result
        if not customer_id:
            result.subscription_error = "Pago aprobado; activación pendiente por falta de identidad."
        else:
            result = await _activate_subscription(payment, job.customer_id, db,
                card_expiration=job.card_expiration, promo_code=job.promo_code or None, user_name=job.user_name)
        job.status = "PENDING" if result.subscription_error else "DONE"
        job.last_error = "Activation requires review" if result.subscription_error else ""
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        result.subscription_error = "Pago aprobado; activación pendiente. No repitas el pago."
        logger.exception("Activation pending payment_id=%s", payment.id)
        return result


async def retry_subscription_activations(session_factory):
    from sqlalchemy import select
    from app.infrastructure.models import SubscriptionActivationJobModel
    from app.infrastructure.repo_impl import SQLPaymentRepository
    async with session_factory() as db:
        from app.services.payment_recovery import recover_payment_operations
        from app.infrastructure.azul_gateway import AzulPaymentGateway
        await recover_payment_operations(db, AzulPaymentGateway())
        ids = list((await db.execute(select(SubscriptionActivationJobModel.payment_id).where(
            SubscriptionActivationJobModel.status == "PENDING").limit(100))).scalars())
        for payment_id in ids:
            payment = await SQLPaymentRepository(db).get_by_id(payment_id)
            if payment is not None:
                job = await db.get(SubscriptionActivationJobModel, payment_id)
                await create_subscription_if_needed(payment, job.customer_id, db)
