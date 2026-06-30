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

import httpx

from app.domain.entities import Payment, PaymentStatus
from app.services.notification_service import send_notification

logger = logging.getLogger(__name__)

_AUTH_API_BASE = os.getenv("AUTH_API_BASE_URL", "https://api.iamatlas.do")


@dataclass
class PostPaymentResult:
    """Summary of all post-payment actions taken."""
    email_sent: bool = False
    card_saved: bool = False
    confirmation_triggered: bool = False
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
