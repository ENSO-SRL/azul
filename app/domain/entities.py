"""
Domain entities — pure data classes with no infrastructure dependencies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class IsoCode(str, Enum):
    """Azul IsoCode values returned in every transaction response."""
    APPROVED           = "00"    # Única respuesta de éxito
    THREE_DS_CHALLENGE = "3D"    # Tarjetahabiente debe completar challenge 3DS
    DECLINED_FUNDS     = "51"    # Fondos insuficientes / declinada
    NOT_AUTHENTICATED  = "08"    # ACS del emisor no disponible en 3DS
    ERROR_GENERIC      = "99"    # Error genérico (CVC, tarjeta inválida, 3DS failed)
    SECURITY_VIOLATION = "63"    # Violación de seguridad
    UNKNOWN            = ""      # Respuesta sin IsoCode (error de validación pre-procesador)

    @classmethod
    def _missing_(cls, value: object) -> "IsoCode":
        """Return UNKNOWN for any code not explicitly defined."""
        return cls.UNKNOWN


class AzulResponseCode(str, Enum):
    """Top-level ResponseCode from Azul.

    ISO8583 → llegó al procesador, revisar IsoCode.
    Error   → fallo antes del procesador (credenciales, payload inválido).
    """
    ISO8583 = "ISO8583"
    ERROR   = "Error"
    UNKNOWN = ""

    @classmethod
    def _missing_(cls, value: object) -> "AzulResponseCode":
        return cls.UNKNOWN


class PaymentStatus(str, Enum):
    PENDING  = "PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    ERROR    = "ERROR"


class PaymentType(str, Enum):
    SALE      = "SALE"
    SERVICE   = "SERVICE"
    RECURRING = "RECURRING"
    CLUB      = "CLUB"


class SubscriptionStatus(str, Enum):
    ACTIVE    = "ACTIVE"
    PAUSED    = "PAUSED"
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

    # CIT vs MIT — requerido por Visa/Mastercard para stored credentials
    initiated_by: Literal["cardholder", "merchant"] = "cardholder"

    # Idempotencia — si se provee, un segundo intento con la misma clave
    # retorna el payment original sin reejecutar la transacción
    idempotency_key: str = ""

    # Campos obligatorios desde Azul API v1.2
    cardholder_name: str = ""
    cardholder_email: str = ""

    # Campos que Azul devuelve
    azul_order_id: str = ""
    iso_code: str = ""
    response_code: str = ""
    response_message: str = ""

    # Metadata de servicio (solo para PaymentType.SERVICE)
    service_type: str = ""
    bill_reference: str = ""

    # Token DataVault retornado si save_card=True
    data_vault_token: str = ""

    # Auto-generados
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SavedCard:
    """A tokenized card stored in Azul DataVault.

    Independiente de las suscripciones — un cliente puede tener varias
    tarjetas guardadas y usarlas para pagos CIT on-demand o suscripciones MIT.
    """

    customer_id: str
    token: str              # UUID de DataVault (ej. 129BCAAB-742A-4F64-AF54-8A9F1BAD802C)
    card_brand: str = ""    # VISA, MASTERCARD, etc.
    card_last4: str = ""
    expiration: str = ""    # YYYYMM
    is_default: bool = False

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RecurringPayment:
    """A recurring payment subscription backed by a DataVault token."""

    customer_id: str
    amount: int                                  # centavos por cobro
    itbis: int = 0
    frequency_days: int = 30
    description: str = ""
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE

    # Token de DataVault (se obtiene en el primer cobro o vía POST /tokens)
    data_vault_token: str = ""
    card_brand: str = ""
    card_last4: str = ""

    # Scheduling
    next_charge_at: datetime | None = None
    last_charged_at: datetime | None = None

    # Retry policy — conteo explícito para evitar lógica heurística frágil
    failed_attempts: int = 0
    last_failure_reason: str = ""

    # Auto-generados
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Transaction:
    """Audit log — one row per HTTP call to Azul.

    IMPORTANTE: request_payload NUNCA debe contener el PAN completo.
    El gateway enmascara dígitos 7-15 antes de persistir.
    """

    payment_id: str
    request_payload: str = ""    # JSON string — PAN enmascarado
    response_payload: str = ""   # JSON string
    http_status: int = 0
    iso_code: str = ""
    response_code: str = ""
    response_message: str = ""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
