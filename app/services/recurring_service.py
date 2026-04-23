"""
Recurring payment service — create subscriptions, charge with tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.entities import (
    Payment,
    PaymentType,
    RecurringPayment,
    SubscriptionStatus,
)
from app.domain.repositories import (
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
    ):
        self._payments = payment_repo
        self._recurring = recurring_repo
        self._txns = txn_repo
        self._gw = gateway

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
    ) -> tuple[RecurringPayment, Payment]:
        """First charge + tokenise the card via DataVault.

        Returns the new subscription and the initial payment.
        cardholder_name and cardholder_email are required by Azul API v1.2.
        """

        # 1. Execute first charge with save_token=True (CIT — user present)
        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.RECURRING,
            auth_mode="splitit",
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
        )

        payment, txn = await self._gw.sale(
            payment, card_number, expiration, cvc, save_token=True
        )

        await self._payments.save(payment)
        await self._txns.save(txn)

        # 2. Extract token from the payment entity (populated by gateway)
        token = payment.data_vault_token

        # 3. Create subscription
        now = datetime.now(timezone.utc)
        recurring = RecurringPayment(
            customer_id=customer_id,
            amount=amount,
            itbis=itbis,
            frequency_days=frequency_days,
            description=description,
            data_vault_token=token,
            card_brand=payment.data_vault_token[:4] if token else "",
            card_last4=card_number[-4:] if len(card_number) >= 4 else card_number,
            last_charged_at=now,
            next_charge_at=now + timedelta(days=frequency_days),
        )

        await self._recurring.save(recurring)
        return recurring, payment

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
        sub.next_charge_at = now + timedelta(days=sub.frequency_days)
        await self._recurring.update(sub)

        return payment

    async def get_subscription(self, recurring_id: str) -> RecurringPayment | None:
        return await self._recurring.get_by_id(recurring_id)

    async def cancel_subscription(self, recurring_id: str) -> RecurringPayment:
        sub = await self._recurring.get_by_id(recurring_id)
        if not sub:
            raise ValueError(f"Subscription {recurring_id} not found")
        sub.status = SubscriptionStatus.CANCELLED
        await self._recurring.update(sub)
        return sub
