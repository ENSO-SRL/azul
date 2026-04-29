"""
Notifications router — test y envío manual de notificaciones.

Endpoints
---------
POST /api/v1/notifications/test          Enviar notificación de prueba
GET  /api/v1/notifications/status        Ver configuración actual
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.notification_service import (
    NotificationEvent,
    _ENABLED,
    _FROM_EMAIL,
    ctx_charge,
    send_notification,
)

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])


class TestNotificationRequest(BaseModel):
    event: str = Field(
        ...,
        description=(
            "Evento a probar: charge_approved | charge_failed | subscription_paused | "
            "subscription_cancelled | card_expired | upcoming_charge"
        ),
    )
    to_email: str = Field(..., description="Email destinatario para la prueba")
    amount: int   = Field(5000,  description="Monto en centavos (ej. 5000 = $50.00)")
    currency: str = Field("DOP", description="DOP o USD")
    description: str = Field("Membresía mensual", description="Nombre de la suscripción")
    card_last4: str  = Field("5872", description="Últimos 4 dígitos de la tarjeta")

    model_config = {"json_schema_extra": {"examples": [
        {
            "event": "charge_approved",
            "to_email": "cliente@ejemplo.com",
            "amount": 5000,
            "currency": "DOP",
            "description": "Membresía mensual",
            "card_last4": "5872",
        }
    ]}}


class TestNotificationResponse(BaseModel):
    sent: bool
    event: str
    to_email: str
    message: str


@router.post(
    "/test",
    response_model=TestNotificationResponse,
    summary="Enviar notificación de prueba",
    description=(
        "Envía un email de prueba para un evento específico. "
        "Si NOTIFY_ENABLED=0 o FROM_EMAIL no está configurado, retorna igualmente "
        "`sent: true` pero el email se loguea en consola en vez de enviarse. "
        "Útil para verificar el diseño de los templates."
    ),
)
async def test_notification(body: TestNotificationRequest):
    _valid_events = {
        "charge_approved", "charge_failed", "subscription_paused",
        "subscription_cancelled", "card_expired", "upcoming_charge",
    }
    if body.event not in _valid_events:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid event. Must be one of: {sorted(_valid_events)}",
        )

    from datetime import datetime, timedelta, timezone
    ctx = ctx_charge(
        amount=body.amount,
        currency=body.currency,
        description=body.description,
        card_last4=body.card_last4,
        next_charge_date=datetime.now(timezone.utc) + timedelta(days=3),
        failure_reason="Fondos insuficientes (simulado)" if "fail" in body.event else "",
        failed_attempts=2 if "fail" in body.event else 0,
    )
    sent = await send_notification(body.event, body.to_email, ctx)  # type: ignore[arg-type]
    return {
        "sent": sent,
        "event": body.event,
        "to_email": body.to_email,
        "message": (
            "Email enviado via SES." if sent and _ENABLED
            else "Logged to console (NOTIFY_ENABLED=0 or SES not configured)."
        ),
    }


@router.get(
    "/status",
    summary="Estado de la configuración de notificaciones",
)
async def notification_status():
    """Retorna si las notificaciones están activas y el email remitente configurado."""
    return {
        "enabled": _ENABLED,
        "from_email": _FROM_EMAIL or "(no configurado — set NOTIFY_FROM_EMAIL en .env)",
        "mode": "AWS SES" if _ENABLED else "log-only (desarrollo)",
        "events_supported": [
            "charge_approved",
            "charge_failed",
            "subscription_paused",
            "subscription_cancelled",
            "card_expired",
            "upcoming_charge",
        ],
    }
