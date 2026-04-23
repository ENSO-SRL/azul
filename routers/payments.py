"""
Payment endpoints — single payments and service payments.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import SQLPaymentRepository, SQLTransactionRepository
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SaleRequest(BaseModel):
    amount: int      = Field(..., description="Monto en centavos (ej. 1000 = $10.00)")
    itbis: int       = Field(0,   description="ITBIS en centavos")
    card_number: str = Field(..., description="Número de tarjeta")
    expiration: str  = Field(..., description="Expiración YYYYMM (ej. 202812)")
    cvc: str         = Field(..., description="CVC / CVV")
    order_id: str    = Field("",  description="Referencia de orden")
    auth_mode: str   = Field("splitit", description="splitit o 3dsecure")
    save_card: bool  = Field(False, description="Si True, tokeniza la tarjeta en DataVault")
    # Obligatorios desde Azul API v1.2
    cardholder_name: str  = Field(..., description="Nombre del tarjetahabiente")
    cardholder_email: str = Field(..., description="Correo electrónico del tarjetahabiente")

    model_config = {"json_schema_extra": {"examples": [
        {
            "amount": 1180,
            "itbis": 180,
            "card_number": "4260550061845872",
            "expiration": "202812",
            "cvc": "123",
            "order_id": "ORD-001",
            "save_card": False,
            "cardholder_name": "Juan Pérez",
            "cardholder_email": "juan@ejemplo.com",
        }
    ]}}


class ServicePaymentRequest(BaseModel):
    amount: int       = Field(..., description="Monto en centavos")
    itbis: int        = Field(0,   description="ITBIS en centavos")
    card_number: str
    expiration: str
    cvc: str
    service_type: str = Field(..., description="Tipo de servicio (ej. electricidad, agua)")
    bill_reference: str = Field(..., description="Referencia de factura")
    order_id: str = ""
    cardholder_name: str  = Field(..., description="Nombre del tarjetahabiente")
    cardholder_email: str = Field(..., description="Correo electrónico del tarjetahabiente")


class PaymentResponse(BaseModel):
    id: str
    order_id: str
    amount: int
    itbis: int
    payment_type: str
    status: str
    iso_code: str
    response_code: str
    response_message: str
    azul_order_id: str
    data_vault_token: str
    created_at: str


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def _get_service(db: AsyncSession = Depends(get_db)) -> PaymentService:
    return PaymentService(
        payment_repo=SQLPaymentRepository(db),
        txn_repo=SQLTransactionRepository(db),
        gateway=AzulPaymentGateway(),
        card_repo=SQLSavedCardRepository(db),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=PaymentResponse,
    summary="Procesar pago único (Sale)",
    description=(
        "Ejecuta un cobro CIT con datos completos de tarjeta. "
        "Usa el header **Idempotency-Key** para reintentos seguros sin cobros duplicados. "
        "Activa ``save_card=true`` para guardar la tarjeta en DataVault y recibir el token."
    ),
)
async def create_payment(
    body: SaleRequest,
    idempotency_key: str  = Header("", alias="Idempotency-Key"),
    svc: PaymentService   = Depends(_get_service),
):
    payment = await svc.process_sale(
        amount=body.amount,
        itbis=body.itbis,
        card_number=body.card_number,
        expiration=body.expiration,
        cvc=body.cvc,
        order_id=body.order_id,
        auth_mode=body.auth_mode,
        save_card=body.save_card,
        idempotency_key=idempotency_key,
        cardholder_name=body.cardholder_name,
        cardholder_email=body.cardholder_email,
    )
    return _to_response(payment)


@router.post(
    "/service",
    response_model=PaymentResponse,
    summary="Pago de servicio (utility bill)",
)
async def create_service_payment(
    body: ServicePaymentRequest,
    idempotency_key: str  = Header("", alias="Idempotency-Key"),
    svc: PaymentService   = Depends(_get_service),
):
    payment = await svc.process_service_payment(
        amount=body.amount,
        itbis=body.itbis,
        card_number=body.card_number,
        expiration=body.expiration,
        cvc=body.cvc,
        service_type=body.service_type,
        bill_reference=body.bill_reference,
        order_id=body.order_id,
        idempotency_key=idempotency_key,
        cardholder_name=body.cardholder_name,
        cardholder_email=body.cardholder_email,
    )
    return _to_response(payment)


@router.get(
    "/{payment_id}",
    response_model=PaymentResponse,
    summary="Consultar pago por ID",
)
async def get_payment(
    payment_id: str,
    svc: PaymentService = Depends(_get_service),
):
    payment = await svc.get_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return _to_response(payment)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_response(p) -> dict:
    return {
        "id": p.id,
        "order_id": p.order_id,
        "amount": p.amount,
        "itbis": p.itbis,
        "payment_type": p.payment_type.value if hasattr(p.payment_type, "value") else p.payment_type,
        "status": p.status.value if hasattr(p.status, "value") else p.status,
        "iso_code": p.iso_code,
        "response_code": p.response_code,
        "response_message": p.response_message,
        "azul_order_id": p.azul_order_id,
        "data_vault_token": p.data_vault_token,
        "created_at": p.created_at.isoformat(),
    }
