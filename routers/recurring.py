"""
Recurring payment endpoints — subscriptions + manual charges.

Routes
------
POST   /api/v1/recurring                    Create subscription (CIT STANDING_ORDER)
GET    /api/v1/recurring                    List subscriptions for a customer
GET    /api/v1/recurring/customer-status     Customer subscription & payment status
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

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Currency, Payment, PaymentStatus, PaymentType
from app.infrastructure.azul_config import load_azul_config
from app.infrastructure.azul_gateway import (
    AzulIntegrationError,
    AzulPaymentGateway,
    _datavault_fields,
)
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
    amount: int = Field(..., ge=1, description="Total a cobrar en centavos, ITBIS incluido (ej. 59000 = RD$590)")
    itbis: int | None = Field(
        None,
        ge=0,
        description=(
            "ITBIS en centavos (porción incluida en 'amount'). "
            "Si se omite, se calcula automáticamente como el 18% incluido en 'amount'. "
            "Envía 0 explícito para suscripciones exentas."
        ),
    )
    card_number: str = Field(..., description="Número de tarjeta (se tokeniza)")
    expiration: str = Field(..., description="Expiración YYYYMM")
    cvc: str
    frequency_days: int = Field(30, description="Frecuencia de cobro en días")
    trial_days: int = Field(0, description="Días de prueba gratuita (sin cobro inicial)")
    description: str = Field("", description="Descripción de la suscripción")
    currency: str    = Field("DOP", description="Moneda: DOP (peso dominicano) o USD")
    cardholder_name: str  = Field(..., description="Nombre del tarjetahabiente")
    cardholder_email: str = Field(..., description="Correo electrónico del tarjetahabiente")
    auth_mode: str = Field("splitit", description="splitit o 3dsecure")
    browser_info: BrowserInfoSchema | None = Field(
        None, description="Datos del navegador — obligatorio si auth_mode=3dsecure",
    )

    model_config = {"json_schema_extra": {"examples": [
        {"customer_id": "CLI-001", "amount": 59000,
         "card_number": "4260550061845872", "expiration": "202812", "cvc": "123",
         "frequency_days": 30, "description": "Membresía mensual (RD$590, ITBIS incluido auto)",
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
    currency: str = "DOP"
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
    trial_ends_at: str | None = None
    in_trial: bool = False
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


class SubscriptionStatusDetail(BaseModel):
    subscription_id: str
    description: str
    amount: int
    status: str
    is_current: bool
    is_overdue: bool
    overdue_reason: str
    failed_attempts: int
    next_charge_at: str | None
    last_charged_at: str | None
    card_last4: str
    in_trial: bool = False
    trial_ends_at: str | None = None


class CustomerStatusResponse(BaseModel):
    """Estado consolidado de suscripciones y pagos de un cliente."""
    customer_id: str
    has_subscriptions: bool
    is_active: bool = Field(description="True si tiene al menos una suscripción ACTIVE")
    is_current: bool = Field(description="True si está al día con TODOS los pagos")
    has_overdue_payment: bool = Field(description="True si alguna suscripción tiene pago vencido o fallido")
    in_trial: bool = Field(False, description="True si alguna suscripción activa está en período de gracia")
    trial_ends_at: str | None = Field(None, description="Fecha ISO en que termina el período de gracia")
    total_subscriptions: int
    active_count: int
    paused_count: int
    cancelled_count: int
    subscriptions: list[SubscriptionStatusDetail]


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
    from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
    return RecurringService(
        payment_repo=SQLPaymentRepository(db),
        recurring_repo=SQLRecurringRepository(db),
        txn_repo=SQLTransactionRepository(db),
        gateway=AzulPaymentGateway(),
        consent_repo=SQLConsentRepository(db),
        card_repo=SQLSavedCardRepository(db),
        db_session=db,
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
            trial_days=body.trial_days,
            currency=body.currency,
        )
    except AzulIntegrationError as e:
        raise HTTPException(status_code=503, detail=f"Error de integración con Azul: {e}")
    except Exception as e:
        if str(e).startswith("CONFLICT:"):
            raise HTTPException(status_code=409, detail=str(e).replace("CONFLICT: ", ""))
        raise HTTPException(status_code=400, detail=str(e))
    resp = _to_sub_response(recurring)
    resp["initial_payment_id"] = initial_payment.id if initial_payment else ""
    resp["initial_payment_status"] = (
        initial_payment.status.value
        if initial_payment and hasattr(initial_payment.status, "value")
        else getattr(initial_payment, "status", "") if initial_payment else ""
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
    "/customer-status",
    response_model=CustomerStatusResponse,
    summary="Estado de suscripción y pagos de un cliente",
)
async def get_customer_status(
    customer_id: str,
    svc: RecurringService = Depends(_get_service),
):
    """Retorna si el cliente está activo y al día con sus pagos.

    Campos clave de la respuesta:
    - **is_active**: `true` si tiene al menos una suscripción ACTIVE.
    - **is_current**: `true` si está al día con TODOS los pagos (ningún cobro pendiente/fallido).
    - **has_overdue_payment**: `true` si alguna suscripción tiene pago vencido o con intentos fallidos.

    Para cada suscripción individual incluye `is_current` e `is_overdue` con el detalle.
    """
    return await svc.get_customer_status(customer_id)


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
    except AzulIntegrationError as e:
        raise HTTPException(status_code=503, detail=f"Error de integración con Azul: {e}")
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
# Diagnóstico — causa raíz de los cobros recurrentes (MIT / 3DS) en producción
# ---------------------------------------------------------------------------

class MitDiagRequest(BaseModel):
    """Parámetros del experimento A/B para aislar el 3DS en cobros MIT."""
    token: str = Field(
        "",
        description="DataVaultToken existente (tuyo). Recomendado — NO genera cobro de tokenización.",
    )
    card_number: str = Field("", description="PAN para tokenizar (opción 2 — genera 1 cobro real).")
    expiration: str = Field("", description="Expiración YYYYMM (ej. 203012).")
    cvc: str = Field("", description="CVC.")
    amount: int = Field(100, ge=1, description="Monto en centavos (default 100 = RD$1.00).")
    itbis: int = Field(0, ge=0, description="ITBIS en centavos (default 0).")
    cardholder_name: str = Field("Diag MIT", description="CardHolderName.")
    cardholder_email: str = Field("diag@atlas.do", description="CardHolderEmail.")
    confirm: bool = Field(
        False,
        description="Debe ser true para ejecutar transacciones reales (cobra dinero real en producción).",
    )

    model_config = {"json_schema_extra": {"examples": [
        {"token": "76212E91-79AC-4E60-81C3-961EA5479B91", "amount": 100, "confirm": True}
    ]}}


def _diag_new_payment(body: MitDiagRequest) -> Payment:
    """Payment con OrderNumber corto (<=15) y CustomOrderId único por intento."""
    short = uuid.uuid4().hex[:8].upper()
    return Payment(
        id=f"diag-mit-{short}",           # CustomOrderId único (evita idempotencia de AZUL)
        order_id=f"DIAG{short[:6]}",      # OrderNumber corto y válido
        amount=body.amount,
        itbis=body.itbis,
        payment_type=PaymentType.RECURRING,
        auth_mode="splitit",
        initiated_by="merchant",
        currency_code=Currency.DOP,
        cardholder_name=body.cardholder_name,
        cardholder_email=body.cardholder_email,
    )


def _diag_build_mit_payload(gw: AzulPaymentGateway, payment: Payment, token: str, force_no3ds: bool) -> dict:
    """Replica el payload de sale_mit(), con ForceNo3DS conmutable."""
    payload = gw._base_payload(payment)
    payload.update({
        "CardNumber": "",
        "Expiration": "",
        "CVC": "",
        **_datavault_fields(False, token),
        "merchantInitiatedIndicator": "STANDING_ORDER",
    })
    if force_no3ds:
        payload["ForceNo3DS"] = "1"
    return payload


async def _diag_attempt(gw: AzulPaymentGateway, payment: Payment, token: str, force_no3ds: bool) -> dict:
    """Ejecuta un MIT (sin persistir en DB) y devuelve el resultado estructurado."""
    payload = _diag_build_mit_payload(gw, payment, token, force_no3ds)
    res: dict = {
        "force_no3ds_sent": force_no3ds,
        "custom_order_id": payload.get("CustomOrderId"),
        "order_number": payload.get("OrderNumber"),
    }
    try:
        p, _txn = await gw._execute(payment, payload)
        res.update({
            "response_code": p.response_code,
            "iso_code": p.iso_code,
            "response_message": p.response_message,
            "azul_order_id": p.azul_order_id,
            "authorization_code": p.authorization_code,
            "status": p.status.value,
            "charged": p.status == PaymentStatus.APPROVED,
            "error": None,
        })
    except AzulIntegrationError as e:
        res.update({
            "response_code": "Error",
            "iso_code": "",
            "response_message": "",
            "status": "INTEGRATION_ERROR",
            "charged": False,
            "error": str(e),
        })
    return res


def _diag_verdict(a: dict, b: dict) -> dict:
    """Mapea el resultado A/B a la causa raíz y la acción recomendada."""
    a_iso = a.get("iso_code") or ""
    b_err = b.get("error") or ""
    a_3ds = a_iso in ("3D2METHOD", "3D")

    if a.get("charged"):
        code = "MIT_OK_SIN_FORCE"
        conclusion = "El MIT aprueba SIN ForceNo3DS — la causa raíz NO es 3DS en el comercio."
        action = "Revisar el task/credenciales del proceso que falla (posible deploy viejo con OrderNumber inválido)."
    elif a_3ds and "ForceNo3DS" in b_err:
        code = "COMERCIO_SIN_MIT_NO_3DS"
        conclusion = (
            "CONFIRMADO: el comercio de producción rechaza ForceNo3DS y fuerza 3DS 2.0 en el MIT, "
            "que un cobro headless no puede completar."
        )
        action = "AZUL/Luis Recio debe habilitar MIT stored-credential sin 3DS en el merchant. Ningún cambio de código lo resuelve solo."
    elif a_3ds and b.get("charged"):
        code = "SOLO_STRIP_CODIGO"
        conclusion = "Con ForceNo3DS=1 el MIT aprueba; el único bloqueante era el strip de _force_no3ds en producción."
        action = "Revertir el strip de _force_no3ds para producción y redesplegar. No hace falta AZUL."
    else:
        code = "NO_CONCLUYENTE"
        conclusion = "Resultado fuera del árbol esperado."
        action = "Revisar el detalle de attempt_a / attempt_b (response_code, iso_code, error)."

    return {"code": code, "conclusion": conclusion, "recommended_action": action}


@router.post(
    "/diagnostics/mit-3ds",
    tags=["Debug"],
    summary="Diagnóstico causa raíz: MIT con/sin ForceNo3DS (cobra dinero real)",
    description=(
        "Dispara el MISMO cobro MIT dos veces contra AZUL — una SIN ForceNo3DS "
        "(comportamiento actual de prod) y otra CON ForceNo3DS=1 (como se certificó "
        "en sandbox) — y devuelve un veredicto de causa raíz.\n\n"
        "⚠️ Ejecuta transacciones REALES. Requiere `confirm=true`. El intento A "
        "normalmente no cobra (vuelve 3D2METHOD); el intento B cobra sólo si aprueba. "
        "Usá un `token` propio para no generar cobro de tokenización.\n\n"
        "No persiste nada en la base — es una sonda pura contra el gateway."
    ),
)
async def diagnose_mit_3ds(body: MitDiagRequest):
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Debe enviar confirm=true — este endpoint ejecuta cobros reales en AZUL.",
        )

    cfg = load_azul_config()
    gw = AzulPaymentGateway()

    result: dict = {
        "env": cfg.env,
        "merchant_id": cfg.merchant_id,
        "api_url": cfg.api_url,
        "note": "Cada intento usa un CustomOrderId distinto; no se persiste en la base.",
    }

    # Resolver token: usar el provisto, o tokenizar la tarjeta (genera 1 cobro).
    token = body.token.strip()
    if not token:
        if not (body.card_number and body.expiration and body.cvc):
            raise HTTPException(
                status_code=400,
                detail="Falta 'token' (recomendado) o 'card_number'+'expiration'+'cvc' para tokenizar.",
            )
        tok_payment = _diag_new_payment(body)
        tok_payment.payment_type = PaymentType.SALE
        tok_payment.initiated_by = "cardholder"
        try:
            tok_payment, _ = await gw.sale(
                tok_payment, body.card_number, body.expiration, body.cvc, save_token=True,
            )
        except AzulIntegrationError as e:
            raise HTTPException(status_code=503, detail=f"Tokenización falló (integration error): {e}")
        token = tok_payment.data_vault_token or ""
        result["tokenization"] = {
            "iso_code": tok_payment.iso_code,
            "status": tok_payment.status.value,
            "azul_order_id": tok_payment.azul_order_id,
            "token_obtained": bool(token),
        }
        if not token:
            result["verdict"] = {
                "code": "TOKENIZACION_FALLIDA",
                "conclusion": (
                    "No se pudo tokenizar la tarjeta (iso=%s). Si volvió 3D2METHOD, el comercio "
                    "fuerza 3DS incluso en CIT con PAN." % (tok_payment.iso_code or "∅")
                ),
                "recommended_action": "Reintentá con un 'token' existente para completar el test MIT.",
            }
            return result

    # Experimento A/B (dos CustomOrderId distintos).
    attempt_a = await _diag_attempt(gw, _diag_new_payment(body), token, force_no3ds=False)
    attempt_b = await _diag_attempt(gw, _diag_new_payment(body), token, force_no3ds=True)

    result["attempt_a_sin_forceno3ds"] = attempt_a
    result["attempt_b_con_forceno3ds"] = attempt_b
    result["verdict"] = _diag_verdict(attempt_a, attempt_b)
    return result


# ---------------------------------------------------------------------------
# Diagnóstico — procedencia del token (¿la CIT que lo creó estableció bien la
# credencial almacenada?). Read-only: reconstruye la CIT de origen desde la
# base, sin cobrar ni llamar a AZUL. Cierra el eslabón CIT→MIT.
# ---------------------------------------------------------------------------

class TokenProvenanceRequest(BaseModel):
    token: str = Field("", description="DataVaultToken a investigar.")
    subscription_id: str = Field("", description="Alternativa: id de suscripción (se toma su token).")

    model_config = {"json_schema_extra": {"examples": [
        {"token": "BA3F81D4-B4AF-40AC-96F6-9B5FB12E07E5"}
    ]}}


def _diag_parse_indicators(request_payload: str) -> dict:
    """Extrae los indicadores relevantes del request JSON almacenado (enmascarado)."""
    import json as _json
    out = {
        "trx_type": None,
        "cardholderInitiatedIndicator": None,
        "merchantInitiatedIndicator": None,
        "ForceNo3DS": None,
        "SaveToDataVault": None,
        "sent_ThreeDSAuth": False,
    }
    try:
        d = _json.loads(request_payload or "{}")
    except Exception:  # noqa: BLE001
        return out
    if isinstance(d, dict):
        out["trx_type"] = d.get("TrxType")
        out["cardholderInitiatedIndicator"] = d.get("cardholderInitiatedIndicator")
        out["merchantInitiatedIndicator"] = d.get("merchantInitiatedIndicator")
        out["ForceNo3DS"] = d.get("ForceNo3DS")
        out["SaveToDataVault"] = d.get("SaveToDataVault")
        out["sent_ThreeDSAuth"] = "ThreeDSAuth" in d
    return out


def _diag_provenance_verdict(creator, cit: dict) -> dict:
    """Evalúa si el token proviene de una CIT stored-credential válida."""
    if creator is None:
        return {
            "code": "SIN_CIT_EN_BASE",
            "conclusion": "No hay un Payment en la base que haya creado este token (SaveToDataVault). "
                          "El token no fue generado por este sistema, o su registro fue purgado.",
            "recommended_action": "Verificá el origen del token; probá con uno de una suscripción reciente.",
        }
    iso = (creator.get("iso_code") or "")
    chi = (cit.get("cardholderInitiatedIndicator") or "")
    save = (cit.get("SaveToDataVault") or "")
    approved = iso == "00"
    marked = chi == "STANDING_ORDER"
    saved = save == "1"

    if approved and marked and saved:
        return {
            "code": "CIT_OK_STORED_CREDENTIAL",
            "conclusion": "El token proviene de una CIT APROBADA (IsoCode=00) con "
                          "cardholderInitiatedIndicator=STANDING_ORDER y SaveToDataVault=1 — "
                          "es una credencial almacenada correctamente establecida.",
            "recommended_action": "Si el MIT sobre este token igual devuelve 3D2METHOD, "
                                  "la falla es de AZUL: el merchant no honra la exención MIT. "
                                  "Enviar a AZUL los dos AzulOrderId + RRN de la CIT.",
        }
    if not approved:
        return {
            "code": "CIT_NO_APROBADA",
            "conclusion": f"La CIT que creó el token NO fue aprobada (IsoCode={iso or '∅'}). "
                          "La credencial pudo no quedar registrada.",
            "recommended_action": "El problema está antes del MIT: revisar por qué la CIT no llega a 00.",
        }
    return {
        "code": "CIT_MAL_MARCADA",
        "conclusion": "El token fue creado por una operación que NO es una CIT recurrente correcta "
                      f"(cardholderInitiatedIndicator={chi or '∅'}, SaveToDataVault={save or '∅'}). "
                      "La relación stored-credential pudo no establecerse.",
        "recommended_action": "Problema del lado de Atlas: asegurar que el alta use sale_recurring_cit "
                              "(STANDING_ORDER + SaveToDataVault=1) y que la CIT autentique.",
    }


@router.post(
    "/diagnostics/token-provenance",
    tags=["Debug"],
    summary="Diagnóstico read-only: ¿la CIT que creó el token estableció bien la credencial?",
    description=(
        "Reconstruye desde la base la transacción CIT que generó un DataVaultToken "
        "(SaveToDataVault=1) y reporta sus indicadores (cardholderInitiatedIndicator, "
        "ForceNo3DS), IsoCode, AzulOrderId, RRN y AuthorizationCode.\n\n"
        "Es de solo lectura: NO cobra ni llama a AZUL. Sirve para separar 'token mal "
        "establecido' (lado Atlas) de 'AZUL no honra la exención MIT' (lado AZUL)."
    ),
)
async def diagnose_token_provenance(
    body: TokenProvenanceRequest,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select
    from app.infrastructure.models import PaymentModel, TransactionModel

    token = body.token.strip()
    if not token and body.subscription_id.strip():
        sub = await SQLRecurringRepository(db).get_by_id(body.subscription_id.strip())
        token = sub.data_vault_token if sub else ""
    if not token:
        raise HTTPException(status_code=400, detail="Falta 'token' o 'subscription_id' con token.")

    # Payment(s) cuyo response trajo este DataVaultToken = la(s) CIT que lo crearon.
    res = await db.execute(
        select(PaymentModel)
        .where(PaymentModel.data_vault_token == token)
        .order_by(PaymentModel.created_at.asc())
    )
    creators = res.scalars().all()
    creator_model = creators[0] if creators else None

    creator = None
    cit_indicators: dict = {}
    if creator_model is not None:
        creator = {
            "payment_id": creator_model.id,
            "iso_code": creator_model.iso_code,
            "status": creator_model.status,
            "payment_type": creator_model.payment_type,
            "initiated_by": creator_model.initiated_by,
            "azul_order_id": creator_model.azul_order_id,
            "rrn": getattr(creator_model, "rrn", ""),
            "authorization_code": getattr(creator_model, "authorization_code", ""),
            "customer_id": getattr(creator_model, "customer_id", ""),
            "created_at": creator_model.created_at.isoformat() if creator_model.created_at else None,
        }
        # Transacción de auditoría con el request enviado a AZUL (enmascarado).
        tres = await db.execute(
            select(TransactionModel)
            .where(TransactionModel.payment_id == creator_model.id)
            .order_by(TransactionModel.created_at.asc())
        )
        for t in tres.scalars().all():
            parsed = _diag_parse_indicators(t.request_payload)
            # Nos quedamos con la que efectivamente tokenizó (SaveToDataVault=1) si existe.
            if parsed.get("SaveToDataVault") == "1" or not cit_indicators:
                cit_indicators = parsed

    return {
        "token_masked": f"{token[:8]}…{token[-4:]}" if len(token) >= 12 else token,
        "creator_count": len(creators),
        "creating_cit": creator,
        "cit_request_indicators": cit_indicators,
        "verdict": _diag_provenance_verdict(creator, cit_indicators),
        "note": "Read-only: reconstruido desde pagos/transacciones locales; no se llamó a AZUL.",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_sub_response(r) -> dict:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    trial_ends = getattr(r, "trial_ends_at", None)
    in_trial = bool(trial_ends and trial_ends > now) if hasattr(r, "trial_ends_at") else False
    return {
        "id": r.id,
        "customer_id": r.customer_id,
        "amount": r.amount,
        "itbis": r.itbis,
        "frequency_days": r.frequency_days,
        "description": r.description,
        "status": r.status.value if hasattr(r.status, "value") else r.status,
        "currency": (
            r.currency_code.value if hasattr(getattr(r, "currency_code", None), "value")
            else getattr(r, "currency_code", None) or "DOP"
        ),
        "card_brand": getattr(r, "card_brand", ""),
        "card_last4": r.card_last4,
        "card_expiration": getattr(r, "card_expiration", ""),
        "data_vault_token": r.data_vault_token,
        "next_charge_at": r.next_charge_at.isoformat() if r.next_charge_at else None,
        "last_charged_at": r.last_charged_at.isoformat() if r.last_charged_at else None,
        "failed_attempts": getattr(r, "failed_attempts", 0),
        "last_failure_reason": getattr(r, "last_failure_reason", ""),
        "trial_ends_at": trial_ends.isoformat() if trial_ends else None,
        "in_trial": in_trial,
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
