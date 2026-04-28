"""
3DS 2.0 endpoints — callbacks from ACS and frontend continuation.

Endpoints:
    POST /api/v1/3ds/method-notification  — ACS notifica que el iframe 3DS Method terminó
    POST /api/v1/3ds/term                 — ACS devuelve resultado del challenge
    GET  /api/v1/3ds/{payment_id}/status  — Frontend consulta estado 3DS del pago
    POST /api/v1/3ds/{payment_id}/complete-method    — Frontend indica que el método terminó
    POST /api/v1/3ds/{payment_id}/complete-challenge — Frontend indica que el challenge terminó
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import SQLPaymentRepository, SQLTransactionRepository
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.payment_service import PaymentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/3ds", tags=["3DS 2.0"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ThreeDSStatusResponse(BaseModel):
    payment_id: str
    status: str
    iso_code: str
    response_message: str
    azul_order_id: str
    threeds_method_form: str = ""
    threeds_redirect_url: str = ""
    threeds_challenge_form: str = ""
    data_vault_token: str = ""


class CompleteMethodRequest(BaseModel):
    method_notification_status: str = Field(
        "RECEIVED",
        description="RECEIVED | EXPECTED_BUT_NOT_RECEIVED | NOT_EXPECTED",
    )


class ThreeDSPaymentResponse(BaseModel):
    payment_id: str
    status: str
    iso_code: str
    response_message: str
    data_vault_token: str = ""
    threeds_redirect_url: str = ""
    threeds_challenge_form: str = ""


class TermCallbackRequest(BaseModel):
    cres: str = Field("", description="cRes devuelto por el ACS")


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
# ACS Callbacks (llamados por el banco / ACS, no por tu frontend)
# ---------------------------------------------------------------------------

_METHOD_NOTIFICATION_RECEIVED: dict[str, bool] = {}


@router.post(
    "/method-notification",
    summary="MethodNotificationUrl — callback del ACS",
    description=(
        "El ACS/emisor hace POST aquí cuando el iframe 3DS Method terminó. "
        "Recibe el payment_id como query param. El body puede contener "
        "threeDSMethodData del ACS. Responde 200 OK vacío."
    ),
)
async def method_notification(
    payment_id: str = Query(..., description="ID del pago"),
    three_ds_method_data: str = Form(default="", alias="threeDSMethodData"),
):
    """ACS calls this after the 3DS Method iframe completes."""
    logger.info(
        "[3ds] method-notification received for payment_id=%s has_data=%s",
        payment_id,
        bool(three_ds_method_data),
    )
    _METHOD_NOTIFICATION_RECEIVED[payment_id] = True
    return Response(status_code=200)


@router.post(
    "/term",
    summary="TermUrl — callback final del ACS (challenge)",
    description=(
        "El banco redirige al tarjetahabiente aquí después del challenge. "
        "Recibe payment_id como query param y opcionalmente CRes en el body."
    ),
)
async def term_callback(
    payment_id: str = Query(..., description="ID del pago"),
    cres: str = Form(default="", alias="cRes"),
    svc: PaymentService = Depends(_get_service),
):
    """ACS redirects here after the cardholder completes the challenge."""
    logger.info("[3ds] term callback received for payment_id=%s has_cres=%s", payment_id, bool(cres))
    try:
        payment = await svc.continue_three_ds_challenge(payment_id, cres=cres)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "payment_id": payment.id,
        "status": payment.status.value,
        "iso_code": payment.iso_code,
        "response_message": payment.response_message,
        "data_vault_token": payment.data_vault_token,
    }


# ---------------------------------------------------------------------------
# Frontend-facing endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/{payment_id}/status",
    response_model=ThreeDSStatusResponse,
    summary="Consultar estado 3DS del pago",
    description=(
        "El frontend usa este endpoint para verificar en qué paso del flujo "
        "3DS se encuentra el pago y obtener los datos necesarios para "
        "renderizar iframes o redirigir."
    ),
)
async def get_threeds_status(
    payment_id: str,
    svc: PaymentService = Depends(_get_service),
):
    payment = await svc.get_payment(payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return {
        "payment_id": payment.id,
        "status": payment.status.value,
        "iso_code": payment.iso_code,
        "response_message": payment.response_message,
        "azul_order_id": payment.azul_order_id,
        "threeds_method_form": payment.threeds_method_form,
        "threeds_redirect_url": payment.threeds_redirect_url,
        "threeds_challenge_form": payment.threeds_challenge_form,
        "data_vault_token": payment.data_vault_token,
    }


@router.post(
    "/{payment_id}/complete-method",
    response_model=ThreeDSPaymentResponse,
    summary="Continuar flujo 3DS después del iframe Method",
    description=(
        "El frontend llama aquí después de renderizar el MethodForm iframe "
        "y esperar el callback (o timeout de 10s). Envía el "
        "MethodNotificationStatus que corresponda:\n\n"
        "- **RECEIVED**: si el ACS notificó dentro de 10s\n"
        "- **EXPECTED_BUT_NOT_RECEIVED**: si pasaron 10s sin notificación\n"
        "- **NOT_EXPECTED**: si no se envió MethodNotificationUrl"
    ),
)
async def complete_method(
    payment_id: str,
    body: CompleteMethodRequest,
    svc: PaymentService = Depends(_get_service),
):
    status = body.method_notification_status
    if _METHOD_NOTIFICATION_RECEIVED.pop(payment_id, False) and status != "RECEIVED":
        status = "RECEIVED"

    try:
        payment = await svc.continue_three_ds_method(payment_id, status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "payment_id": payment.id,
        "status": payment.status.value,
        "iso_code": payment.iso_code,
        "response_message": payment.response_message,
        "data_vault_token": payment.data_vault_token,
        "threeds_redirect_url": payment.threeds_redirect_url,
        "threeds_challenge_form": payment.threeds_challenge_form,
    }


@router.post(
    "/{payment_id}/complete-challenge",
    response_model=ThreeDSPaymentResponse,
    summary="Completar flujo 3DS después del challenge",
    description=(
        "El frontend llama aquí después de que el tarjetahabiente completó "
        "el challenge del banco (o fue redirigido de vuelta por el TermUrl). "
        "Este endpoint finaliza la autenticación y completa el pago."
    ),
)
async def complete_challenge(
    payment_id: str,
    body: TermCallbackRequest,
    svc: PaymentService = Depends(_get_service),
):
    try:
        payment = await svc.continue_three_ds_challenge(payment_id, cres=body.cres)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "payment_id": payment.id,
        "status": payment.status.value,
        "iso_code": payment.iso_code,
        "response_message": payment.response_message,
        "data_vault_token": payment.data_vault_token,
        "threeds_redirect_url": "",
        "threeds_challenge_form": "",
    }
