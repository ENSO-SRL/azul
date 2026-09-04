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
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import (
    ConsentRecord,
    Currency,
    Payment,
    PaymentStatus,
    PaymentType,
    RecurringPayment,
    SavedCard,
    SubscriptionStatus,
)
from app.domain.repositories import (
    ConsentRepository,
    PaymentRepository,
    RecurringRepository,
    SavedCardRepository,
    TransactionRepository,
)
from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.services.notification_service import ctx_charge, send_notification


class RecurringService:

    def __init__(
        self,
        payment_repo: PaymentRepository,
        recurring_repo: RecurringRepository,
        txn_repo: TransactionRepository,
        gateway: AzulPaymentGateway,
        consent_repo: ConsentRepository | None = None,
        card_repo: SavedCardRepository | None = None,
        db_session: AsyncSession | None = None,
    ):
        self._payments   = payment_repo
        self._recurring  = recurring_repo
        self._txns       = txn_repo
        self._gw         = gateway
        self._consents   = consent_repo  # optional — only needed for consent endpoints
        self._card_repo  = card_repo
        self._db         = db_session

    async def _get_search_ids(self, customer_id: str) -> set[str]:
        search_ids = {customer_id}
        if self._db:
            from sqlalchemy import text
            try:
                if customer_id.isdigit():
                    result = await self._db.execute(
                        text("SELECT email FROM public.users WHERE id = :cid LIMIT 1"),
                        {"cid": int(customer_id)},
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        search_ids.add(row[0])
                else:
                    result = await self._db.execute(
                        text("SELECT id FROM public.users WHERE email = :email LIMIT 1"),
                        {"email": customer_id},
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        search_ids.add(str(row[0]))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Error fetching user cross-reference: {e}")
        return search_ids

    # ------------------------------------------------------------------
    # Subscription creation (CIT — first charge + tokenise)
    # ------------------------------------------------------------------

    async def create_subscription(
        self,
        customer_id: str,
        amount: int,
        itbis: int | None,
        card_number: str,
        expiration: str,
        cvc: str,
        frequency_days: int = 30,
        description: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        auth_mode: str = "splitit",
        browser_info: dict[str, str] | None = None,
        trial_days: int = 0,
        currency: str = "DOP",
    ) -> tuple[RecurringPayment, Payment | None]:
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
        
        # Resolve currency once — used by the first charge AND persisted on the
        # subscription so every future MIT charge uses the same currency.
        try:
            curr = Currency((currency or "DOP").upper())
        except ValueError:
            raise ValueError(f"VALIDATION_ERROR:currency — moneda no soportada: {currency!r} (use DOP o USD)")

        # ITBIS: 'amount' es el TOTAL a cobrar (impuesto incluido). Si no se
        # provee itbis explícito, se desglosa automáticamente la porción de ITBIS
        # contenida en el total. Azul cobra 'amount' — el itbis es informativo.
        from app.utils.tax import included_itbis
        if itbis is None:
            itbis = included_itbis(amount)
        elif itbis > amount:
            raise ValueError(
                f"VALIDATION_ERROR:itbis — el ITBIS ({itbis}) no puede exceder el total ({amount})."
            )

        # Guard: check if customer already has an active subscription for this plan
        existing_subs = await self._recurring.list_by_customer(customer_id)
        for sub in existing_subs:
            if sub.status == SubscriptionStatus.ACTIVE and sub.description == description:
                raise ValueError("CONFLICT: El usuario ya tiene una suscripción activa para este plan.")

        if trial_days > 0:
            try:
                saved = await self._gw.create_token(
                    card_number=card_number,
                    expiration=expiration,
                    cvc=cvc,
                    customer_id=customer_id,
                    cardholder_name=cardholder_name,
                    cardholder_email=cardholder_email,
                )
            except ValueError as exc:
                if "VALIDATION_ERROR:TrxType" in str(exc):
                    raise ValueError("DataVault CREATE no está habilitado en sandbox. El trial requiere TrxType=CREATE habilitado en Azul.")
                raise
            
            if self._card_repo:
                local_card = SavedCard(
                    customer_id=customer_id,
                    token=saved.token,
                    card_brand=saved.card_brand,
                    card_last4=saved.card_last4,
                    expiration=expiration,
                    is_default=True
                )
                await self._card_repo.save(local_card)

            now = datetime.now(timezone.utc)
            recurring = RecurringPayment(
                customer_id=customer_id,
                amount=amount,
                itbis=itbis,
                frequency_days=frequency_days,
                description=description,
                currency_code=curr,
                card_last4=card_number[-4:] if len(card_number) >= 4 else card_number,
                card_expiration=expiration,
                data_vault_token=saved.token,
                next_charge_at=now + timedelta(days=trial_days),
                trial_ends_at=now + timedelta(days=trial_days),
                last_charged_at=None,
            )
            await self._recurring.save(recurring)
            return recurring, None

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.RECURRING,
            auth_mode=auth_mode,
            currency_code=curr,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            initiated_by="cardholder",
            customer_id=customer_id,
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
                currency_code=curr,
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
                currency_code=curr,
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
            currency_code=curr,
            data_vault_token=token,
            card_brand=payment.card_number_masked[:4] if payment.card_number_masked else "",
            card_last4=card_number[-4:] if len(card_number) >= 4 else card_number,
            card_expiration=expiration,
            cardholder_email=cardholder_email,
            last_charged_at=now,
            next_charge_at=now + timedelta(days=frequency_days),
        )

        await self._recurring.save(recurring)

        # Send the invoice/receipt for the FIRST charge (previously missing for
        # subscriptions created via the API — only checkout sent a receipt).
        from app.services.scheduler import notify_charge_outcome
        await notify_charge_outcome(recurring, payment, invoice_number=payment.id)

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

        from app.services.scheduler import (
            build_custom_order_id,
            _handle_failure,
            notify_charge_outcome,
            verify_prior_charge,
        )

        # Full-date cycle key (YYYYMMDD) so two charges in the same calendar month
        # do not collide on the same CustomOrderId (see scheduler for rationale).
        cycle = (
            sub.next_charge_at.strftime("%Y%m%d")
            if sub.next_charge_at
            else datetime.now(timezone.utc).strftime("%Y%m%d")
        )
        custom_order_id = build_custom_order_id(sub.id, sub.failed_attempts, cycle)

        # ── Idempotency guard (same two layers as the scheduler) ─────────────
        # Prevents a double charge if this cycle was already paid (e.g. the
        # scheduler and a manual charge race, or a retry after a crash).
        existing = await self._payments.get_by_id(custom_order_id)
        db_ok = existing is not None and existing.status == PaymentStatus.APPROVED
        azul_prior = None if db_ok else await verify_prior_charge(self._gw, custom_order_id)
        if db_ok or azul_prior is not None:
            now = datetime.now(timezone.utc)
            sub.failed_attempts = 0
            sub.last_failure_reason = ""
            sub.last_charged_at = existing.created_at if existing is not None else now
            sub.next_charge_at = now + timedelta(days=sub.frequency_days)
            await self._recurring.update(sub)
            if existing is not None:
                return existing
            recovered = Payment(
                id=custom_order_id, order_id=f"REC-{uuid.uuid4().hex[:8].upper()}",
                amount=sub.amount, itbis=sub.itbis,
                payment_type=PaymentType.RECURRING, auth_mode="splitit",
                initiated_by="merchant", currency_code=sub.currency_code,
                customer_id=sub.customer_id,
            )
            recovered.status = PaymentStatus.APPROVED
            recovered.iso_code = "00"
            azul_data = azul_prior or {}
            recovered.azul_order_id = (
                azul_data.get("AzulOrderId") or azul_data.get("AZULOrderId", "")
            )
            recovered.authorization_code = azul_data.get("AuthorizationCode", "")
            recovered.response_message = "Recuperado vía verify_payment (idempotencia)"
            await self._payments.save(recovered)
            return recovered

        payment = Payment(
            id=custom_order_id,
            order_id=f"REC-{uuid.uuid4().hex[:8].upper()}",
            amount=sub.amount,
            itbis=sub.itbis,
            payment_type=PaymentType.RECURRING,
            auth_mode="splitit",
            initiated_by="merchant",
            currency_code=sub.currency_code,
            customer_id=sub.customer_id,
        )

        # MIT — user not present, use stored token
        payment, txn = await self._gw.sale_mit(payment, sub.data_vault_token)

        await self._payments.save(payment)
        await self._txns.save(txn)

        now = datetime.now(timezone.utc)
        if payment.status == PaymentStatus.APPROVED:
            # Only advance the billing schedule on a successful charge.
            sub.failed_attempts = 0
            sub.last_failure_reason = ""
            sub.last_charged_at = now
            sub.next_charge_at  = now + timedelta(days=sub.frequency_days)
        else:
            # Declined — apply the same retry/backoff policy as the scheduler.
            # Do NOT advance next_charge_at or mark the cycle as paid.
            reason = payment.response_message or f"IsoCode={payment.iso_code}"
            sub = _handle_failure(sub, reason)

        await self._recurring.update(sub)

        # Notify the customer (success invoice / decline / pause) — same behaviour
        # as the scheduler, so manual charges are no longer silent.
        await notify_charge_outcome(sub, payment, invoice_number=custom_order_id)

        return payment

    # ------------------------------------------------------------------
    # CRUD / status transitions
    # ------------------------------------------------------------------

    async def get_subscription(self, recurring_id: str) -> RecurringPayment | None:
        return await self._recurring.get_by_id(recurring_id)

    async def list_subscriptions(self, customer_id: str) -> list[RecurringPayment]:
        """Return all subscriptions for a customer (any status)."""
        search_ids = await self._get_search_ids(customer_id)
        subs = []
        for sid in search_ids:
            subs.extend(await self._recurring.list_by_customer(sid))
            
        seen_ids = set()
        unique_subs = []
        for s in subs:
            if s.id not in seen_ids:
                seen_ids.add(s.id)
                unique_subs.append(s)
        return unique_subs

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

        # Notify customer of cancellation
        await send_notification(
            "subscription_cancelled",
            to_email=sub.cardholder_email,
            context=ctx_charge(
                amount=sub.amount,
                currency=sub.currency_code.value if hasattr(sub.currency_code, "value") else "DOP",
                description=sub.description or "Suscripción",
                card_last4=sub.card_last4,
            ),
        )
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

    # ------------------------------------------------------------------
    # Customer subscription status
    # ------------------------------------------------------------------

    async def get_customer_status(self, customer_id: str) -> dict:
        """Return a consolidated payment/subscription status for a customer.

        Evaluates all subscriptions and returns whether the customer is
        active, current with payments, or has overdue charges.

        Status logic per subscription:
        - ACTIVE + failed_attempts == 0 + next_charge_at >= now → ✅ current
        - ACTIVE + failed_attempts > 0 → ⚠️ has failed charges (overdue)
        - ACTIVE + next_charge_at < now → ⚠️ payment overdue (scheduler hasn't charged yet)
        - PAUSED → ⛔ suspended (usually after retry exhaustion)
        - CANCELLED → ❌ no longer active

        Trial (grace period):
        - trial_ends_at is set and in the future → in_trial = True
        """
        search_ids = await self._get_search_ids(customer_id)
        subs = []
        for sid in search_ids:
            subs.extend(await self._recurring.list_by_customer(sid))
            
        seen_ids = set()
        unique_subs = []
        for s in subs:
            if s.id not in seen_ids:
                seen_ids.add(s.id)
                unique_subs.append(s)
        subs = unique_subs

        if not subs:
            return {
                "customer_id": customer_id,
                "has_subscriptions": False,
                "is_active": False,
                "is_current": False,
                "has_overdue_payment": False,
                "in_trial": False,
                "trial_ends_at": None,
                "total_subscriptions": 0,
                "active_count": 0,
                "paused_count": 0,
                "cancelled_count": 0,
                "subscriptions": [],
            }

        now = datetime.now(timezone.utc)

        active_subs = [s for s in subs if s.status == SubscriptionStatus.ACTIVE]
        paused_subs = [s for s in subs if s.status == SubscriptionStatus.PAUSED]
        cancelled_subs = [s for s in subs if s.status == SubscriptionStatus.CANCELLED]

        subscription_details = []
        any_overdue = False
        any_in_trial = False
        trial_end_date = None

        for s in subs:
            is_overdue = False
            reason = ""

            if s.status == SubscriptionStatus.ACTIVE:
                if s.failed_attempts > 0:
                    is_overdue = True
                    reason = f"Cobro fallido ({s.failed_attempts} intento(s)): {s.last_failure_reason}"
                elif s.next_charge_at and s.next_charge_at < now:
                    is_overdue = True
                    reason = "Pago vencido — pendiente de cobro automático"

            if is_overdue:
                any_overdue = True

            # Trial detection
            sub_in_trial = bool(
                s.trial_ends_at
                and s.trial_ends_at > now
                and s.status == SubscriptionStatus.ACTIVE
            )
            if sub_in_trial:
                any_in_trial = True
                trial_end_date = s.trial_ends_at

            subscription_details.append({
                "subscription_id": s.id,
                "description": s.description,
                "amount": s.amount,
                "status": s.status.value if hasattr(s.status, "value") else s.status,
                "is_current": s.status == SubscriptionStatus.ACTIVE and not is_overdue,
                "is_overdue": is_overdue,
                "overdue_reason": reason,
                "failed_attempts": s.failed_attempts,
                "next_charge_at": s.next_charge_at.isoformat() if s.next_charge_at else None,
                "last_charged_at": s.last_charged_at.isoformat() if s.last_charged_at else None,
                "card_last4": s.card_last4,
                "in_trial": sub_in_trial,
                "trial_ends_at": s.trial_ends_at.isoformat() if s.trial_ends_at else None,
            })

        has_active = len(active_subs) > 0

        return {
            "customer_id": customer_id,
            "has_subscriptions": True,
            "is_active": has_active,
            "is_current": has_active and not any_overdue,
            "has_overdue_payment": any_overdue,
            "in_trial": any_in_trial,
            "trial_ends_at": trial_end_date.isoformat() if trial_end_date else None,
            "total_subscriptions": len(subs),
            "active_count": len(active_subs),
            "paused_count": len(paused_subs),
            "cancelled_count": len(cancelled_subs),
            "subscriptions": subscription_details,
        }
