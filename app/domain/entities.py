"""
Domain entities — pure data classes with no infrastructure dependencies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    ERROR = "ERROR"


class PaymentType(str, Enum):
    SALE = "SALE"
    SERVICE = "SERVICE"
    RECURRING = "RECURRING"


class SubscriptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass
class Payment:
    """A single payment attempt."""

    amount: int                                  # centavos (e.g. 1000 = $10.00)
    itbis: int = 0                               # impuesto en centavos
    payment_type: PaymentType = PaymentType.SALE
    status: PaymentStatus = PaymentStatus.PENDING
    order_id: str = ""
    card_number_masked: str = ""                 # últimos 4 dígitos
    currency: str = "$"
    auth_mode: str = "splitit"

    # Campos que Azul devuelve
    azul_order_id: str = ""
    iso_code: str = ""
    response_message: str = ""

    # Metadata de servicio (solo para PaymentType.SERVICE)
    service_type: str = ""
    bill_reference: str = ""

    # Auto-generados
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RecurringPayment:
    """A recurring payment subscription backed by a DataVault token."""

    customer_id: str
    amount: int                                  # centavos por cobro
    itbis: int = 0
    frequency_days: int = 30
    description: str = ""
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE

    # Token de DataVault (se obtiene en el primer cobro)
    data_vault_token: str = ""
    card_brand: str = ""
    card_last4: str = ""

    # Scheduling
    next_charge_at: datetime | None = None
    last_charged_at: datetime | None = None

    # Auto-generados
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Transaction:
    """Audit log — one row per HTTP call to Azul."""

    payment_id: str
    request_payload: str = ""    # JSON string
    response_payload: str = ""   # JSON string
    http_status: int = 0
    iso_code: str = ""
    response_message: str = ""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
