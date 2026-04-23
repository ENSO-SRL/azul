"""
Payment service — orchestrates single payments and service payments.

Idempotency
-----------
If an ``idempotency_key`` is provided and a payment with that key already
exists in the database, the service returns the existing record WITHOUT
calling Azul again.  This prevents double charges on client retries.
"""

from __future__ import annotations

from app.domain.entities import Payment, PaymentStatus, PaymentType
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
    ) -> Payment:
        """Create and execute a one-time CIT Sale.

        If ``save_card=True`` the card is stored in DataVault and the token
        is persisted on the Payment record for future use.
        """
        # Idempotency check
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
        )

        payment, txn = await self._gw.sale(
            payment, card_number, expiration, cvc, save_token=save_card
        )

        # If tokenized, persist SavedCard separately
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
    # Helpers
    # ------------------------------------------------------------------

    async def get_payment(self, payment_id: str) -> Payment | None:
        return await self._payments.get_by_id(payment_id)
