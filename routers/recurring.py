"""
Recurring payment endpoints — subscriptions + manual charges.

Routes
------
POST   /api/v1/recurring                    Create subscription (CIT STANDING_ORDER)
GET    /api/v1/recurring                    List subscriptions for a customer
GET    /api/v1/recurring/{id}               Get subscription detail
POST   /api/v1/recurring/{id}/charge        Manual MIT charge
POST   /api/v1/recurring/{id}/pause         Pause subscription
POST   /api/v1/recurring/{id}/resume        Resume paused subscription
POST   /api/v1/recurring/{id}/consent       Record customer consent (Visa/MC requirement)
GET    /api/v1/recurring/{id}/consent       Check consent record
GET    /api/v1/recurring/{id}/history       Charge history for a subscription
DELETE /api/v1/recurring/{id}               Cancel + DataVault DELETE
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import (
    SQLConsentRepository,
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
    currency: str    = Field("DOP", description="Moneda: DOP (peso dominicano) o USD")
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
    card_brand: str
    card_last4: str
    card_expiration: str
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


class ConsentRequest(BaseModel):
    consent_text: str = Field(
        ...,
        description="Texto exacto mostrado al cliente, ej. 'Acepto que se me cobre RD$500 cada 30 días hasta cancelar'",
    )
    ip_address: str = Field("", description="IP del cliente al momento del consentimiento")
    user_agent: str = Field("", description="User-Agent del navegador del cliente")

    model_config = {"json_schema_extra": {"examples": [
        {
            "consent_text": "Acepto que se me cobre RD$500.00 cada 30 días hasta cancelar.",
            "ip_address": "192.168.1.1",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
        }
    ]}}


class ConsentResponse(BaseModel):
    id: str
    subscription_id: str
    customer_id: str
    consent_text: str
    ip_address: str
    user_agent: str
    consented_at: str


class TransactionHistoryItem(BaseModel):
    id: str
    payment_id: str
    iso_code: str
    response_code: str
    response_message: str
    http_status: int
    created_at: str


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def _get_service(db: AsyncSession = Depends(get_db)) -> RecurringService:
    return RecurringService(
        payment_repo=SQLPaymentRepository(db),
        recurring_repo=SQLRecurringRepository(db),
        txn_repo=SQLTransactionRepository(db),
        gateway=AzulPaymentGateway(),
        consent_repo=SQLConsentRepository(db),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=SubscriptionResponse,
    status_code=201,
    summary="Crear suscripción recurrente (primer cobro CIT STANDING_ORDER)",
)
async def create_subscription(
    body: CreateSubscriptionRequest,
    svc: RecurringService = Depends(_get_service),
):
    """Ejecuta el primer cobro con ``cardholderInitiatedIndicator: STANDING_ORDER``,
    tokeniza la tarjeta con DataVault, y crea la suscripción recurrente.

    El indicador STANDING_ORDER en el primer cobro le avisa a Visa/MC que habrá
    cobros futuros merchant-initiated — mejora las tasas de aprobación.
    """
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


@router.get(
    "",
    response_model=list[SubscriptionResponse],
    summary="Listar suscripciones de un cliente",
)
async def list_subscriptions(
    customer_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Retorna todas las suscripciones de un cliente (cualquier estado)."""
    subs = await svc.list_subscriptions(customer_id)
    return [_to_sub_response(s) for s in subs]


@router.get(
    "/{recurring_id}",
    response_model=SubscriptionResponse,
    summary="Ver suscripción",
)
async def get_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    sub = await svc.get_subscription(recurring_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return _to_sub_response(sub)


@router.post(
    "/{recurring_id}/charge",
    response_model=ChargeResponse,
    summary="Cobrar suscripción manualmente (MIT)",
)
async def charge_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Ejecuta un cobro MIT usando el token almacenado de DataVault.
    El cliente NO está presente — se usa ``merchantInitiatedIndicator: STANDING_ORDER``.
    """
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


@router.post(
    "/{recurring_id}/pause",
    response_model=SubscriptionResponse,
    summary="Pausar suscripción",
)
async def pause_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Pausa la suscripción sin eliminar el token DataVault.
    Puede reanudarse con ``POST /{id}/resume``.
    """
    try:
        sub = await svc.pause_subscription(recurring_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_sub_response(sub)


@router.post(
    "/{recurring_id}/resume",
    response_model=SubscriptionResponse,
    summary="Reanudar suscripción pausada",
)
async def resume_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Reanuda una suscripción pausada y resetea el contador de intentos fallidos."""
    try:
        sub = await svc.resume_subscription(recurring_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_sub_response(sub)


@router.delete(
    "/{recurring_id}",
    response_model=SubscriptionResponse,
    summary="Cancelar suscripción (+ DataVault DELETE)",
)
async def cancel_subscription(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Cancela la suscripción y elimina el token del vault de AZUL.

    Llama ``TrxType=DELETE`` en DataVault para cumplimiento GDPR.
    Si el DELETE falla (red, token ya expirado), la suscripción se cancela
    igualmente — el fallo se registra en logs pero no bloquea la cancelación.
    """
    try:
        sub = await svc.cancel_subscription(recurring_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_sub_response(sub)


@router.post(
    "/{recurring_id}/consent",
    response_model=ConsentResponse,
    status_code=201,
    summary="Registrar consentimiento del cliente (requerido Visa/MC)",
)
async def record_consent(
    recurring_id: str,
    body: ConsentRequest,
    request: Request,
    svc: RecurringService = Depends(_get_service),
):
    """Persiste el consentimiento del tarjetahabiente para cobros recurrentes.

    **Visa y Mastercard exigen evidencia documentada** de que el cliente autorizó
    los cobros futuros. Guarda el texto exacto mostrado, la IP del cliente y el
    timestamp UTC de aceptación.

    Debe llamarse **después del primer cobro exitoso**, en el mismo flujo de
    alta de suscripción desde tu frontend.
    """
    # Use IP from request if not provided in body
    ip = body.ip_address or (request.client.host if request.client else "")
    sub = await svc.get_subscription(recurring_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        consent = await svc.record_consent(
            subscription_id=recurring_id,
            customer_id=sub.customer_id,
            consent_text=body.consent_text,
            ip_address=ip,
            user_agent=body.user_agent,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _to_consent_response(consent)


@router.get(
    "/{recurring_id}/consent",
    response_model=ConsentResponse,
    summary="Obtener registro de consentimiento",
)
async def get_consent(
    recurring_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Retorna el registro de consentimiento almacenado para una suscripción."""
    try:
        consent = await svc.get_consent(recurring_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not consent:
        raise HTTPException(status_code=404, detail="Consent record not found")
    return _to_consent_response(consent)


@router.get(
    "/{recurring_id}/history",
    response_model=list[TransactionHistoryItem],
    summary="Historial de transacciones de una suscripción",
)
async def get_subscription_history(
    recurring_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retorna todos los intentos de cobro (exitosos y fallidos) de una suscripción.

    Los resultados se ordenan por fecha descendente. Útil para reconciliación
    y para mostrar al cliente su historial de pagos.
    """
    from sqlalchemy import select
    from app.infrastructure.models import PaymentModel, TransactionModel

    # Find all payments associated with this subscription via order_id prefix
    result = await db.execute(
        select(TransactionModel)
        .join(PaymentModel, TransactionModel.payment_id == PaymentModel.id)
        .where(PaymentModel.order_id == f"sub-{recurring_id}")
        .order_by(TransactionModel.created_at.desc())
    )
    txns = result.scalars().all()

    # Also include payments created by scheduler (CustomOrderId starts with sub-)
    result2 = await db.execute(
        select(TransactionModel)
        .join(PaymentModel, TransactionModel.payment_id == PaymentModel.id)
        .where(PaymentModel.id.like(f"sub-{recurring_id[:12].replace('-', '')}%"))
        .order_by(TransactionModel.created_at.desc())
    )
    txns2 = result2.scalars().all()

    # Combine and de-duplicate by id
    seen = set()
    all_txns = []
    for t in list(txns) + list(txns2):
        if t.id not in seen:
            seen.add(t.id)
            all_txns.append(t)

    all_txns.sort(key=lambda t: t.created_at, reverse=True)

    return [
        {
            "id": t.id,
            "payment_id": t.payment_id,
            "iso_code": t.iso_code,
            "response_code": getattr(t, "response_code", ""),
            "response_message": t.response_message,
            "http_status": t.http_status,
            "created_at": t.created_at.isoformat(),
        }
        for t in all_txns
    ]


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
        "card_brand": getattr(r, "card_brand", ""),
        "card_last4": r.card_last4,
        "card_expiration": getattr(r, "card_expiration", ""),
        "data_vault_token": r.data_vault_token,
        "next_charge_at": r.next_charge_at.isoformat() if r.next_charge_at else None,
        "last_charged_at": r.last_charged_at.isoformat() if r.last_charged_at else None,
        "failed_attempts": getattr(r, "failed_attempts", 0),
        "last_failure_reason": getattr(r, "last_failure_reason", ""),
        "created_at": r.created_at.isoformat(),
    }


def _to_consent_response(c) -> dict:
    return {
        "id": c.id,
        "subscription_id": c.subscription_id,
        "customer_id": c.customer_id,
        "consent_text": c.consent_text,
        "ip_address": c.ip_address,
        "user_agent": c.user_agent,
        "consented_at": c.consented_at.isoformat(),
    }
