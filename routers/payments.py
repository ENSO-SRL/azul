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

class BrowserInfoSchema(BaseModel):
    """Datos del navegador del tarjetahabiente — requerido para 3DS 2.0."""
    accept_header: str     = Field("text/html", description="Accept header del navegador")
    ip_address: str        = Field(..., description="IP pública del cliente")
    language: str          = Field("es-DO", description="Idioma del navegador")
    color_depth: str       = Field("24", description="Profundidad de color (bits)")
    screen_width: str      = Field("1920", description="Ancho de pantalla en px")
    screen_height: str     = Field("1080", description="Alto de pantalla en px")
    time_zone: str         = Field("240", description="Offset UTC en minutos")
    user_agent: str        = Field(..., description="User-Agent del navegador")
    javascript_enabled: str = Field("true", description="Si JavaScript está activo")


class CardHolderInfoSchema(BaseModel):
    """Bloque CardHolderInfo para enriquecer riesgo 3DS."""
    billing_name: str = ""
    billing_email: str = ""
    phone_home: str = ""
    phone_mobile: str = ""
    phone_work: str = ""
    billing_address1: str = ""
    billing_address2: str = ""
    billing_address3: str = ""
    billing_city: str = ""
    billing_state: str = ""
    billing_zip: str = ""
    billing_country: str = ""
    shipping_address1: str = ""
    shipping_address2: str = ""
    shipping_address3: str = ""
    shipping_city: str = ""
    shipping_state: str = ""
    shipping_zip: str = ""
    shipping_country: str = ""


class SaleRequest(BaseModel):
    amount: int      = Field(..., description="Monto en centavos (ej. 1000 = $10.00)")
    itbis: int       = Field(0,   description="ITBIS en centavos")
    card_number: str = Field(..., description="Número de tarjeta")
    expiration: str  = Field(..., description="Expiración YYYYMM (ej. 202812)")
    cvc: str         = Field(..., description="CVC / CVV")
    order_id: str    = Field("",  description="Referencia de orden")
    auth_mode: str   = Field("splitit", description="splitit o 3dsecure")
    save_card: bool  = Field(False, description="Si True, tokeniza la tarjeta en DataVault")
    cardholder_name: str  = Field(..., description="Nombre del tarjetahabiente")
    cardholder_email: str = Field(..., description="Correo electrónico del tarjetahabiente")
    browser_info: BrowserInfoSchema | None = Field(
        None,
        description="Datos del navegador — obligatorio si auth_mode=3dsecure",
    )
    cardholder_info: CardHolderInfoSchema | None = Field(
        None,
        description="Bloque CardHolderInfo (billing/shipping) recomendado para 3DS",
    )
    requestor_challenge_indicator: str = Field(
        "01",
        description="01/02/03/04 según estrategia 3DS",
    )
    include_method_notification_url: bool = Field(
        True,
        description="Si false, envía MethodNotificationUrl vacío (modo NOT_EXPECTED)",
    )

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
            "browser_info": None,
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


class HoldRequest(BaseModel):
    amount: int
    itbis: int = 0
    card_number: str
    expiration: str
    cvc: str
    order_id: str = ""
    cardholder_name: str = Field(..., description="Nombre del tarjetahabiente")
    cardholder_email: str = Field(..., description="Correo electrónico del tarjetahabiente")


class PostRequest(BaseModel):
    amount: int
    itbis: int = 0
    azul_order_id: str = Field(..., description="AZULOrderId devuelto por Hold")
    card_number: str = Field("", description="Número de tarjeta (requerido por algunos sandbox)")
    expiration: str = Field("", description="Expiración YYYYMM (requerido por algunos sandbox)")
    cvc: str = Field("123", description="CVC requerido por sandbox para Post")
    order_id: str = ""
    cardholder_name: str = ""
    cardholder_email: str = ""


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
    threeds_method_form: str = ""
    threeds_redirect_url: str = ""
    threeds_challenge_form: str = ""
    created_at: str


class VerifyPaymentRequest(BaseModel):
    custom_order_id: str = Field(..., description="CustomOrderId enviado originalmente a Azul")


class VerifyPaymentResponse(BaseModel):
    response_code: str = ""
    response_message: str = ""
    iso_code: str = ""
    azul_order_id: str = ""
    found: bool = False
    raw: dict = Field(default_factory=dict)


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
    browser_info_dict = body.browser_info.model_dump() if body.browser_info else None
    cardholder_info_dict = body.cardholder_info.model_dump() if body.cardholder_info else None
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
        browser_info=browser_info_dict,
        cardholder_info=cardholder_info_dict,
        requestor_challenge_indicator=body.requestor_challenge_indicator,
        include_method_notification_url=body.include_method_notification_url,
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

@router.post(
    "/hold",
    response_model=PaymentResponse,
    summary="Preautorización (TrxType Hold)",
)
async def create_hold(
    body: HoldRequest,
    idempotency_key: str  = Header("", alias="Idempotency-Key"),
    svc: PaymentService   = Depends(_get_service),
):
    payment = await svc.process_hold(
        amount=body.amount,
        itbis=body.itbis,
        card_number=body.card_number,
        expiration=body.expiration,
        cvc=body.cvc,
        order_id=body.order_id,
        cardholder_name=body.cardholder_name,
        cardholder_email=body.cardholder_email,
        idempotency_key=idempotency_key,
    )
    return _to_response(payment)


@router.post(
    "/post",
    response_model=PaymentResponse,
    summary="Captura de preautorización (TrxType Post)",
)
async def create_post(
    body: PostRequest,
    idempotency_key: str  = Header("", alias="Idempotency-Key"),
    svc: PaymentService   = Depends(_get_service),
):
    payment = await svc.process_post(
        amount=body.amount,
        itbis=body.itbis,
        azul_order_id=body.azul_order_id,
        card_number=body.card_number,
        expiration=body.expiration,
        cvc=body.cvc,
        order_id=body.order_id,
        cardholder_name=body.cardholder_name,
        cardholder_email=body.cardholder_email,
        idempotency_key=idempotency_key,
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


@router.post(
    "/verify",
    response_model=VerifyPaymentResponse,
    summary="Verificar una transacción en Azul por CustomOrderId",
)
async def verify_payment(
    body: VerifyPaymentRequest,
    svc: PaymentService = Depends(_get_service),
):
    data = await svc.verify_payment(custom_order_id=body.custom_order_id)
    return {
        "response_code": data.get("ResponseCode", ""),
        "response_message": data.get("ResponseMessage", data.get("ErrorDescription", "")),
        "iso_code": data.get("IsoCode", ""),
        "azul_order_id": data.get("AzulOrderId", ""),
        "found": data.get("Found") in (True, "true", "True"),
        "raw": data,
    }


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
        "threeds_method_form": getattr(p, "threeds_method_form", ""),
        "threeds_redirect_url": getattr(p, "threeds_redirect_url", ""),
        "threeds_challenge_form": getattr(p, "threeds_challenge_form", ""),
        "created_at": p.created_at.isoformat(),
    }
