"""
Recurring payment service — create subscriptions, charge with tokens.

CIT / MIT indicators
--------------------
First charge (create_subscription):
    ``cardholderInitiatedIndicator: "STANDING_ORDER"``
    Signals to Visa/MC that the customer has authorised future recurring
    debits.  Must be paired with SaveToDataVault="1".

Subsequent MIT charges (charge):
    ``merchantInitiatedIndicator: "STANDING_ORDER"``
    The scheduler fires these without the user present.
    ForceNo3DS="1" — 3DS does not apply for MIT.

Cancellation
------------
cancel_subscription() calls DataVault DELETE to remove the stored card
from Azul's vault.  This satisfies GDPR / consumer-rights requirements.
The subscription is then marked CANCELLED.

Consent
-------
record_consent() stores the exact text the customer saw, their IP, and
the timestamp.  Visa/MC require this evidence for recurring charge disputes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.entities import (
    ConsentRecord,
    Payment,
    PaymentStatus,
    PaymentType,
    RecurringPayment,
    SubscriptionStatus,
)
from app.domain.repositories import (
    ConsentRepository,
    PaymentRepository,
    RecurringRepository,
    TransactionRepository,
)
from app.infrastructure.azul_gateway import AzulPaymentGateway


class RecurringService:

    def __init__(
        self,
        payment_repo: PaymentRepository,
        recurring_repo: RecurringRepository,
        txn_repo: TransactionRepository,
        gateway: AzulPaymentGateway,
        consent_repo: ConsentRepository | None = None,
    ):
        self._payments   = payment_repo
        self._recurring  = recurring_repo
        self._txns       = txn_repo
        self._gw         = gateway
        self._consents   = consent_repo  # optional — only needed for consent endpoints

    # ------------------------------------------------------------------
    # Subscription creation (CIT — first charge + tokenise)
    # ------------------------------------------------------------------

    async def create_subscription(
        self,
        customer_id: str,
        amount: int,
        itbis: int,
        card_number: str,
        expiration: str,
        cvc: str,
        frequency_days: int = 30,
        description: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        auth_mode: str = "splitit",
        browser_info: dict[str, str] | None = None,
    ) -> tuple[RecurringPayment, Payment]:
        """First charge + tokenise the card via DataVault (CIT STANDING_ORDER).

        Returns the new subscription and the initial payment.

        The gateway sends ``cardholderInitiatedIndicator: "STANDING_ORDER"``
        (not the generic "1") so Visa/MC know from the very first transaction
        that this is a stored-credential / recurring arrangement.

        When auth_mode="3dsecure" and browser_info is provided the first
        charge goes through 3DS 2.0.  If 3DS requires method or challenge,
        the payment will be PENDING_3DS_METHOD or PENDING_3DS_CHALLENGE and
        the subscription will NOT be saved yet — the caller must complete 3DS
        via /api/v1/3ds/ and then finalise.
        """

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.RECURRING,
            auth_mode=auth_mode,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            initiated_by="cardholder",
        )

        # Use sale_cit_new — STANDING_ORDER CIT + SaveToDataVault=1
        payment, txn = await self._gw.sale_recurring_cit(
            payment, card_number, expiration, cvc,
            browser_info=browser_info,
        )

        await self._payments.save(payment)
        await self._txns.save(txn)

        # 3DS requires additional steps — return a placeholder subscription (not persisted)
        if payment.status.value.startswith("PENDING_3DS"):
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=amount,
                itbis=itbis,
                frequency_days=frequency_days,
                description=description,
                card_last4=card_number[-4:] if len(card_number) >= 4 else card_number,
                card_expiration=expiration,
            )
            return recurring, payment

        # Declined or error — do NOT persist a subscription
        if payment.status != PaymentStatus.APPROVED:
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=amount,
                itbis=itbis,
                frequency_days=frequency_days,
                description=description,
                card_last4=card_number[-4:] if len(card_number) >= 4 else card_number,
                card_expiration=expiration,
            )
            return recurring, payment

        token = payment.data_vault_token

        now = datetime.now(timezone.utc)
        recurring = RecurringPayment(
            customer_id=customer_id,
            amount=amount,
            itbis=itbis,
            frequency_days=frequency_days,
            description=description,
            data_vault_token=token,
            card_brand=payment.card_number_masked[:4] if payment.card_number_masked else "",
            card_last4=card_number[-4:] if len(card_number) >= 4 else card_number,
            card_expiration=expiration,
            last_charged_at=now,
            next_charge_at=now + timedelta(days=frequency_days),
        )

        await self._recurring.save(recurring)
        return recurring, payment

    # ------------------------------------------------------------------
    # Manual MIT charge (used by router + scheduler)
    # ------------------------------------------------------------------

    async def charge(self, recurring_id: str) -> Payment:
        """Manually charge an active subscription using its stored token (MIT).

        This is a Merchant-Initiated Transaction — the user is NOT present.
        The scheduler calls this; it can also be triggered manually via the API.
        """
        sub = await self._recurring.get_by_id(recurring_id)
        if not sub:
            raise ValueError(f"Subscription {recurring_id} not found")
        if sub.status != SubscriptionStatus.ACTIVE:
            raise ValueError(f"Subscription {recurring_id} is {sub.status.value}")
        if not sub.data_vault_token:
            raise ValueError(f"Subscription {recurring_id} has no DataVault token")

        payment = Payment(
            amount=sub.amount,
            itbis=sub.itbis,
            payment_type=PaymentType.RECURRING,
            auth_mode="splitit",
            initiated_by="merchant",
        )

        # MIT — user not present, use stored token
        payment, txn = await self._gw.sale_mit(payment, sub.data_vault_token)

        await self._payments.save(payment)
        await self._txns.save(txn)

        # Update subscription schedule
        now = datetime.now(timezone.utc)
        sub.last_charged_at = now
        sub.next_charge_at  = now + timedelta(days=sub.frequency_days)
        await self._recurring.update(sub)

        return payment

    # ------------------------------------------------------------------
    # CRUD / status transitions
    # ------------------------------------------------------------------

    async def get_subscription(self, recurring_id: str) -> RecurringPayment | None:
        return await self._recurring.get_by_id(recurring_id)

    async def list_subscriptions(self, customer_id: str) -> list[RecurringPayment]:
        """Return all subscriptions for a customer (any status)."""
        return await self._recurring.list_by_customer(customer_id)

    async def pause_subscription(self, recurring_id: str) -> RecurringPayment:
        """Pause an active subscription (no DataVault DELETE — card kept for resume)."""
        sub = await self._recurring.get_by_id(recurring_id)
        if not sub:
            raise ValueError(f"Subscription {recurring_id} not found")
        if sub.status == SubscriptionStatus.CANCELLED:
            raise ValueError(f"Subscription {recurring_id} is already cancelled")
        result = await self._recurring.pause(recurring_id)
        return result  # type: ignore[return-value]

    async def resume_subscription(self, recurring_id: str) -> RecurringPayment:
        """Resume a paused subscription (resets failed_attempts counter)."""
        sub = await self._recurring.get_by_id(recurring_id)
        if not sub:
            raise ValueError(f"Subscription {recurring_id} not found")
        if sub.status == SubscriptionStatus.CANCELLED:
            raise ValueError(f"Cannot resume a cancelled subscription")
        if sub.status == SubscriptionStatus.ACTIVE:
            raise ValueError(f"Subscription {recurring_id} is already active")
        result = await self._recurring.resume(recurring_id)
        return result  # type: ignore[return-value]

    async def cancel_subscription(self, recurring_id: str) -> RecurringPayment:
        """Cancel subscription and delete the DataVault token.

        Calls Azul DataVault DELETE before marking CANCELLED so the card
        data is removed from the vault (GDPR / consumer-rights compliance).
        DataVault DELETE failure is logged but does NOT block cancellation —
        we always mark the subscription as CANCELLED regardless.
        """
        sub = await self._recurring.get_by_id(recurring_id)
        if not sub:
            raise ValueError(f"Subscription {recurring_id} not found")

        # Best-effort DataVault DELETE — do not let a gateway error block cancellation
        if sub.data_vault_token:
            try:
                await self._gw.delete_token(sub.data_vault_token)
            except Exception:
                # Log the error but continue — subscription must be cancelled
                import logging
                logging.getLogger(__name__).warning(
                    "[recurring] DataVault DELETE failed for sub=%s token=%s — "
                    "cancelling subscription anyway.",
                    recurring_id, sub.data_vault_token,
                )

        sub.status = SubscriptionStatus.CANCELLED
        sub.data_vault_token = ""  # clear local reference
        await self._recurring.update(sub)
        return sub

    # ------------------------------------------------------------------
    # Consent registration (Visa/MC requirement)
    # ------------------------------------------------------------------

    async def record_consent(
        self,
        subscription_id: str,
        customer_id: str,
        consent_text: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> ConsentRecord:
        """Persist customer consent evidence for recurring charge authorisation.

        Must be called at enrolment time, AFTER the first successful charge.
        The consent_text must be the exact string shown to the customer in the UI.

        Raises:
            RuntimeError: if ConsentRepository was not injected.
            ValueError: if the subscription does not exist.
        """
        if not self._consents:
            raise RuntimeError(
                "ConsentRepository not injected — pass consent_repo= to RecurringService."
            )

        sub = await self._recurring.get_by_id(subscription_id)
        if not sub:
            raise ValueError(f"Subscription {subscription_id} not found")

        consent = ConsentRecord(
            subscription_id=subscription_id,
            customer_id=customer_id,
            consent_text=consent_text,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return await self._consents.save(consent)

    async def get_consent(self, subscription_id: str) -> ConsentRecord | None:
        """Return the consent record for a subscription, if any."""
        if not self._consents:
            raise RuntimeError("ConsentRepository not injected.")
        return await self._consents.get_by_subscription(subscription_id)
