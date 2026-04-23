"""
Payment service — orchestrates single payments and service payments.
"""

from __future__ import annotations

from app.domain.entities import Payment, PaymentStatus, PaymentType
from app.domain.repositories import PaymentRepository, TransactionRepository
from app.infrastructure.azul_gateway import AzulPaymentGateway


class PaymentService:

    def __init__(
        self,
        payment_repo: PaymentRepository,
        txn_repo: TransactionRepository,
        gateway: AzulPaymentGateway,
    ):
        self._payments = payment_repo
        self._txns = txn_repo
        self._gw = gateway

    async def process_sale(
        self,
        amount: int,
        itbis: int,
        card_number: str,
        expiration: str,
        cvc: str,
        order_id: str = "",
        auth_mode: str = "splitit",
    ) -> Payment:
        """Create and execute a one-time Sale."""

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SALE,
            order_id=order_id,
            auth_mode=auth_mode,
        )

        payment, txn = await self._gw.sale(
            payment, card_number, expiration, cvc
        )

        await self._payments.save(payment)
        await self._txns.save(txn)
        return payment

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
    ) -> Payment:
        """Pay a utility / service bill."""

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SERVICE,
            order_id=order_id,
            auth_mode="splitit",
            service_type=service_type,
            bill_reference=bill_reference,
        )

        payment, txn = await self._gw.sale(
            payment, card_number, expiration, cvc
        )

        await self._payments.save(payment)
        await self._txns.save(txn)
        return payment

    async def get_payment(self, payment_id: str) -> Payment | None:
        return await self._payments.get_by_id(payment_id)
