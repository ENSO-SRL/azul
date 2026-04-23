"""
Domain repository interfaces (ports).

These are abstract base classes that define *what* persistence operations
are available.  Concrete implementations live in infrastructure/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities import Payment, RecurringPayment, Transaction


class PaymentRepository(ABC):

    @abstractmethod
    async def save(self, payment: Payment) -> Payment: ...

    @abstractmethod
    async def get_by_id(self, payment_id: str) -> Payment | None: ...

    @abstractmethod
    async def update(self, payment: Payment) -> Payment: ...


class RecurringRepository(ABC):

    @abstractmethod
    async def save(self, recurring: RecurringPayment) -> RecurringPayment: ...

    @abstractmethod
    async def get_by_id(self, recurring_id: str) -> RecurringPayment | None: ...

    @abstractmethod
    async def update(self, recurring: RecurringPayment) -> RecurringPayment: ...

    @abstractmethod
    async def list_active(self) -> list[RecurringPayment]: ...


class TransactionRepository(ABC):

    @abstractmethod
    async def save(self, txn: Transaction) -> Transaction: ...

    @abstractmethod
    async def list_by_payment(self, payment_id: str) -> list[Transaction]: ...
