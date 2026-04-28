"""
Recurring payment endpoints — subscriptions + manual charges.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import (
    SQLPaymentRepository,
    SQLRecurringRepository,
    SQLTransactionRepository,
)
from app.services.recurring_service import RecurringService

router = APIRouter(prefix="/api/v1/recurring", tags=["Recurring Payments"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class BrowserInfoSchema(BaseModel):
    accept_header: str     = Field("text/html")
    ip_address: str        = Field(...)
    language: str          = Field("es-DO")
    color_depth: str       = Field("24")
    screen_width: str      = Field("1920")
    screen_height: str     = Field("1080")
    time_zone: str         = Field("240")
    user_agent: str        = Field(...)
    javascript_enabled: str = Field("true")


class CreateSubscriptionRequest(BaseModel):
    customer_id: str = Field(..., description="ID del cliente")
    amount: int = Field(..., description="Monto recurrente en centavos")
    itbis: int = Field(0, description="ITBIS en centavos")
    card_number: str = Field(..., description="Número de tarjeta (se tokeniza)")
    expiration: str = Field(..., description="Expiración YYYYMM")
    cvc: str
    frequency_days: int = Field(30, description="Frecuencia de cobro en días")
    description: str = Field("", description="Descripción de la suscripción")
    cardholder_name: str  = Field(..., description="Nombre del tarjetahabiente")
    cardholder_email: str = Field(..., description="Correo electrónico del tarjetahabiente")
    auth_mode: str = Field("splitit", description="splitit o 3dsecure")
    browser_info: BrowserInfoSchema | None = Field(
        None, description="Datos del navegador — obligatorio si auth_mode=3dsecure",
    )

    model_config = {"json_schema_extra": {"examples": [
        {"customer_id": "CLI-001", "amount": 5000, "itbis": 900,
         "card_number": "4260550061845872", "expiration": "202812", "cvc": "123",
         "frequency_days": 30, "description": "Membresía mensual",
         "cardholder_name": "Juan Pérez", "cardholder_email": "juan@ejemplo.com",
         "auth_mode": "splitit", "browser_info": None}
    ]}}


class SubscriptionResponse(BaseModel):
    id: str
    customer_id: str
    amount: int
    itbis: int
    frequency_days: int
    description: str
    status: str
    card_last4: str
    data_vault_token: str
    next_charge_at: str | None
    last_charged_at: str | None
    failed_attempts: int
    last_failure_reason: str
    initial_payment_id: str = ""
    initial_payment_status: str = ""
    created_at: str


class ChargeResponse(BaseModel):
    payment_id: str
    amount: int
    status: str
    iso_code: str
    response_message: str


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def _get_service(db: AsyncSession = Depends(get_db)) -> RecurringService:
    return RecurringService(
        payment_repo=SQLPaymentRepository(db),
        recurring_repo=SQLRecurringRepository(db),
        txn_repo=SQLTransactionRepository(db),
        gateway=AzulPaymentGateway(),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=SubscriptionResponse, summary="Crear suscripción recurrente")
async def create_subscription(
    body: CreateSubscriptionRequest,
    svc: RecurringService = Depends(_get_service),
):
    """Ejecuta el primer cobro, tokeniza la tarjeta con DataVault,
    y crea la suscripción recurrente."""
    browser_info_dict = body.browser_info.model_dump() if body.browser_info else None
    try:
        recurring, initial_payment = await svc.create_subscription(
            customer_id=body.customer_id,
            amount=body.amount,
            itbis=body.itbis,
            card_number=body.card_number,
            expiration=body.expiration,
            cvc=body.cvc,
            frequency_days=body.frequency_days,
            description=body.description,
            cardholder_name=body.cardholder_name,
            cardholder_email=body.cardholder_email,
            auth_mode=body.auth_mode,
            browser_info=browser_info_dict,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    resp = _to_sub_response(recurring)
    resp["initial_payment_id"] = initial_payment.id
    resp["initial_payment_status"] = (
        initial_payment.status.value
        if hasattr(initial_payment.status, "value")
        else initial_payment.status
    )
    return resp


@router.post("/{recurring_id}/charge", response_model=ChargeResponse, summary="Cobrar suscripción manualmente")
async def charge_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Ejecuta un cobro usando el token almacenado de DataVault."""
    try:
        payment = await svc.charge(recurring_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "payment_id": payment.id,
        "amount": payment.amount,
        "status": payment.status.value,
        "iso_code": payment.iso_code,
        "response_message": payment.response_message,
    }


@router.get("/{recurring_id}", response_model=SubscriptionResponse, summary="Ver suscripción")
async def get_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    sub = await svc.get_subscription(recurring_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return _to_sub_response(sub)


@router.delete("/{recurring_id}", response_model=SubscriptionResponse, summary="Cancelar suscripción")
async def cancel_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    try:
        sub = await svc.cancel_subscription(recurring_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_sub_response(sub)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_sub_response(r) -> dict:
    return {
        "id": r.id,
        "customer_id": r.customer_id,
        "amount": r.amount,
        "itbis": r.itbis,
        "frequency_days": r.frequency_days,
        "description": r.description,
        "status": r.status.value if hasattr(r.status, "value") else r.status,
        "card_last4": r.card_last4,
        "data_vault_token": r.data_vault_token,
        "next_charge_at": r.next_charge_at.isoformat() if r.next_charge_at else None,
        "last_charged_at": r.last_charged_at.isoformat() if r.last_charged_at else None,
        "failed_attempts": getattr(r, "failed_attempts", 0),
        "last_failure_reason": getattr(r, "last_failure_reason", ""),
        "created_at": r.created_at.isoformat(),
    }
