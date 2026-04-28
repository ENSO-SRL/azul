"""
Payment service — orchestrates single payments and service payments.

Idempotency
-----------
If an ``idempotency_key`` is provided and a payment with that key already
exists in the database, the service returns the existing record WITHOUT
calling Azul again.  This prevents double charges on client retries.
"""

from __future__ import annotations

from app.domain.entities import (
    IsoCode,
    Payment,
    PaymentStatus,
    PaymentType,
    Transaction,
)
from app.domain.repositories import (
    PaymentRepository,
    SavedCardRepository,
    TransactionRepository,
)
from app.infrastructure.azul_gateway import AzulPaymentGateway


class PaymentService:

    def __init__(
        self,
        payment_repo: PaymentRepository,
        txn_repo: TransactionRepository,
        gateway: AzulPaymentGateway,
        card_repo: SavedCardRepository | None = None,
    ):
        self._payments = payment_repo
        self._txns     = txn_repo
        self._gw       = gateway
        self._cards    = card_repo

    # ------------------------------------------------------------------
    # One-time Sale
    # ------------------------------------------------------------------

    async def process_sale(
        self,
        amount: int,
        itbis: int,
        card_number: str,
        expiration: str,
        cvc: str,
        order_id: str = "",
        auth_mode: str = "splitit",
        save_card: bool = False,
        idempotency_key: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        browser_info: dict[str, str] | None = None,
    ) -> Payment:
        """Create and execute a one-time CIT Sale.

        cardholder_name and cardholder_email are required by Azul API v1.2.
        If save_card=True the card is stored in DataVault and the token
        is persisted on the Payment record for future use.

        When auth_mode="3dsecure", pass browser_info from the client to enable
        3DS 2.0 authentication.  The payment may end in PENDING_3DS_METHOD or
        PENDING_3DS_CHALLENGE — the caller must continue the flow via the
        /api/v1/3ds/ endpoints.
        """
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SALE,
            order_id=order_id,
            auth_mode=auth_mode,
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
        )

        payment, txn = await self._gw.sale(
            payment, card_number, expiration, cvc,
            save_token=save_card,
            browser_info=browser_info,
        )

        if save_card and payment.data_vault_token and self._cards:
            from app.domain.entities import SavedCard
            card = SavedCard(
                customer_id=order_id or payment.id,
                token=payment.data_vault_token,
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
            )
            await self._cards.save(card)

        await self._payments.save(payment)
        await self._txns.save(txn)
        return payment

    # ------------------------------------------------------------------
    # Service / utility payment
    # ------------------------------------------------------------------

    async def process_service_payment(
        self,
        amount: int,
        itbis: int,
        card_number: str,
        expiration: str,
        cvc: str,
        service_type: str,
        bill_reference: str,
        order_id: str = "",
        idempotency_key: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
    ) -> Payment:
        """Pay a utility / service bill."""
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SERVICE,
            order_id=order_id,
            auth_mode="splitit",
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
            service_type=service_type,
            bill_reference=bill_reference,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
        )

        payment, txn = await self._gw.sale(payment, card_number, expiration, cvc)

        await self._payments.save(payment)
        await self._txns.save(txn)
        return payment

    # ------------------------------------------------------------------
    # CIT with token — on-demand club charge
    # ------------------------------------------------------------------

    async def charge_club(
        self,
        customer_id: str,
        club_id: str,
        amount: int,
        itbis: int,
        token: str,
        idempotency_key: str = "",
    ) -> Payment:
        """Cardholder-Initiated charge for a club using a stored token.

        The user is present (e.g. tapped "Pagar" in the app) but doesn't
        re-enter card details.
        """
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.CLUB,
            order_id=f"club-{club_id}",
            auth_mode="splitit",
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
        )

        payment, txn = await self._gw.sale_cit(payment, token)

        await self._payments.save(payment)
        await self._txns.save(txn)
        return payment

    # ------------------------------------------------------------------
    # 3DS 2.0 continuation
    # ------------------------------------------------------------------

    async def continue_three_ds_method(
        self,
        payment_id: str,
        method_notification_status: str = "RECEIVED",
    ) -> Payment:
        """Continue 3DS after the Method iframe completed or timed out.

        Called by the MethodNotificationUrl callback or by frontend polling.
        Updates the payment to APPROVED, PENDING_3DS_CHALLENGE, or DECLINED.
        """
        payment = await self._payments.get_by_id(payment_id)
        if not payment:
            raise ValueError(f"Payment {payment_id} not found")
        if payment.status != PaymentStatus.PENDING_3DS_METHOD:
            raise ValueError(
                f"Payment {payment_id} is {payment.status.value}, expected PENDING_3DS_METHOD"
            )

        data = await self._gw.process_three_ds_method(
            azul_order_id=payment.azul_order_id,
            method_notification_status=method_notification_status,
        )

        iso_raw = data.get("IsoCode", "")
        payment.iso_code = iso_raw
        payment.response_message = data.get("ResponseMessage", "")
        payment.response_code = data.get("ResponseCode", "")

        txn = Transaction(
            payment_id=payment.id,
            request_payload=f'{{"AZULOrderId":"{payment.azul_order_id}","MethodNotificationStatus":"{method_notification_status}"}}',
            response_payload=str(data),
            http_status=200,
            iso_code=iso_raw,
            response_code=payment.response_code,
            response_message=payment.response_message,
        )
        await self._txns.save(txn)

        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
            payment.data_vault_token = data.get("DataVaultToken", payment.data_vault_token)
        elif iso_raw == IsoCode.THREE_DS_CHALLENGE:
            payment.status = PaymentStatus.PENDING_3DS_CHALLENGE
            payment.threeds_redirect_url = data.get("RedirectUrl", "")
            challenge_data = data.get("ThreeDSChallenge", {})
            if isinstance(challenge_data, dict):
                payment.threeds_challenge_form = challenge_data.get("ChallengeForm", "")
                if not payment.threeds_redirect_url:
                    payment.threeds_redirect_url = challenge_data.get("RedirectUrl", "")
        else:
            payment.status = PaymentStatus.DECLINED

        payment.threeds_method_form = ""
        await self._payments.update(payment)
        return payment

    async def continue_three_ds_challenge(
        self,
        payment_id: str,
        cres: str = "",
    ) -> Payment:
        """Complete 3DS after the cardholder finished the ACS challenge.

        Called by the TermUrl callback when the bank redirects back.
        """
        payment = await self._payments.get_by_id(payment_id)
        if not payment:
            raise ValueError(f"Payment {payment_id} not found")
        if payment.status != PaymentStatus.PENDING_3DS_CHALLENGE:
            raise ValueError(
                f"Payment {payment_id} is {payment.status.value}, expected PENDING_3DS_CHALLENGE"
            )

        data = await self._gw.process_three_ds_challenge(
            azul_order_id=payment.azul_order_id,
            cres=cres,
        )

        iso_raw = data.get("IsoCode", "")
        payment.iso_code = iso_raw
        payment.response_message = data.get("ResponseMessage", "")
        payment.response_code = data.get("ResponseCode", "")

        masked_cres = "***" if cres else ""
        txn = Transaction(
            payment_id=payment.id,
            request_payload=f'{{"AZULOrderId":"{payment.azul_order_id}","cRes":"{masked_cres}"}}',
            response_payload=str(data),
            http_status=200,
            iso_code=iso_raw,
            response_code=payment.response_code,
            response_message=payment.response_message,
        )
        await self._txns.save(txn)

        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
            payment.data_vault_token = data.get("DataVaultToken", payment.data_vault_token)
        else:
            payment.status = PaymentStatus.DECLINED

        payment.threeds_redirect_url = ""
        payment.threeds_challenge_form = ""
        await self._payments.update(payment)
        return payment

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def get_payment(self, payment_id: str) -> Payment | None:
        return await self._payments.get_by_id(payment_id)
