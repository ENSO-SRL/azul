"""
Domain repository interfaces (ports).

These are abstract base classes that define *what* persistence operations
are available.  Concrete implementations live in infrastructure/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities import ConsentRecord, Payment, RecurringPayment, SavedCard, Transaction


class PaymentRepository(ABC):

    @abstractmethod
    async def save(self, payment: Payment) -> Payment: ...

    @abstractmethod
    async def get_by_id(self, payment_id: str) -> Payment | None: ...

    @abstractmethod
    async def update(self, payment: Payment) -> Payment: ...

    @abstractmethod
    async def find_by_idempotency_key(self, key: str) -> Payment | None:
        """Return an existing payment with the given idempotency key, or None."""
        ...


class RecurringRepository(ABC):

    @abstractmethod
    async def save(self, recurring: RecurringPayment) -> RecurringPayment: ...

    @abstractmethod
    async def get_by_id(self, recurring_id: str) -> RecurringPayment | None: ...

    @abstractmethod
    async def update(self, recurring: RecurringPayment) -> RecurringPayment: ...

    @abstractmethod
    async def list_active(self) -> list[RecurringPayment]: ...

    @abstractmethod
    async def list_due(self) -> list[RecurringPayment]:
        """Return active subscriptions whose next_charge_at <= now."""
        ...

    @abstractmethod
    async def list_by_customer(self, customer_id: str) -> list[RecurringPayment]:
        """Return all subscriptions (any status) for a given customer."""
        ...

    @abstractmethod
    async def pause(self, recurring_id: str) -> RecurringPayment | None:
        """Set status=PAUSED.  Returns the updated entity or None if not found."""
        ...

    @abstractmethod
    async def resume(self, recurring_id: str) -> RecurringPayment | None:
        """Set status=ACTIVE and reset failed_attempts.  Returns updated entity or None."""
        ...


class TransactionRepository(ABC):

    @abstractmethod
    async def save(self, txn: Transaction) -> Transaction: ...

    @abstractmethod
    async def list_by_payment(self, payment_id: str) -> list[Transaction]: ...


class SavedCardRepository(ABC):

    @abstractmethod
    async def save(self, card: SavedCard) -> SavedCard: ...

    @abstractmethod
    async def get_by_token(self, token: str) -> SavedCard | None: ...

    @abstractmethod
    async def list_by_customer(self, customer_id: str) -> list[SavedCard]: ...

    @abstractmethod
    async def delete(self, token: str) -> None: ...


class ConsentRepository(ABC):
    """Persistence port for customer consent records."""

    @abstractmethod
    async def save(self, consent: ConsentRecord) -> ConsentRecord:
        """Persist a new consent record."""
        ...

    @abstractmethod
    async def get_by_subscription(self, subscription_id: str) -> ConsentRecord | None:
        """Return the consent record for a subscription, or None."""
        ...
