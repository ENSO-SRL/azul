"""
Atlas Notification Service — email transaccional vía AWS SES.

Envía emails al cliente en los eventos clave del ciclo de pagos.

Configuración (.env)
--------------------
NOTIFY_FROM_EMAIL   dirección remitente (ej. atlas@tu-dominio.com)
NOTIFY_AWS_REGION   región SES (default: us-east-1)
NOTIFY_ENABLED      1 = activo | 0 = solo logs (default: 1 si FROM_EMAIL está definido)

Fallback
--------
Si NOTIFY_FROM_EMAIL no está definido o NOTIFY_ENABLED=0, todas las
llamadas loguean el mensaje y retornan sin error — útil en desarrollo.

Eventos soportados
------------------
charge_approved     Cobro MIT exitoso
charge_failed       Cobro MIT declinado
subscription_paused Suscripción pausada (3 fallos o tarjeta vencida)
subscription_cancelled  Cliente canceló
card_expired        Tarjeta vencida detectada por el scheduler
upcoming_charge     Aviso 3 días antes del próximo cobro
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_FROM_EMAIL   = os.getenv("NOTIFY_FROM_EMAIL", "")
_AWS_REGION   = os.getenv("NOTIFY_AWS_REGION", "us-east-1")
_ENABLED      = os.getenv("NOTIFY_ENABLED", "1" if _FROM_EMAIL else "0") == "1"

NotificationEvent = Literal[
    "charge_approved",
    "charge_failed",
    "subscription_paused",
    "subscription_cancelled",
    "card_expired",
    "upcoming_charge",
]


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------

def _render(event: NotificationEvent, ctx: dict) -> tuple[str, str]:
    """Return (subject, html_body) for the given event and context."""

    atlas_blue  = "#0057FF"
    atlas_dark  = "#0A0E1A"
    amount_dop  = f"RD${ctx.get('amount_display', '0.00')}"
    currency    = ctx.get('currency', 'DOP')
    if currency == 'USD':
        amount_dop = f"US${ctx.get('amount_display', '0.00')}"

    sub_name    = ctx.get("description", "tu suscripción")
    next_date   = ctx.get("next_charge_date", "")
    fail_reason = ctx.get("failure_reason", "")
    card_last4  = ctx.get("card_last4", "****")
    attempts    = ctx.get("failed_attempts", "")

    _header = f"""
    <div style="background:{atlas_dark};padding:24px 32px;border-radius:12px 12px 0 0;">
      <h1 style="color:{atlas_blue};font-family:Inter,Arial,sans-serif;font-size:22px;margin:0;">
        Atlas Pagos
      </h1>
    </div>
    <div style="background:#fff;padding:32px;border-radius:0 0 12px 12px;font-family:Inter,Arial,sans-serif;color:#1a1a2e;">
    """
    _footer = """
      <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px;">
      <p style="color:#888;font-size:12px;">
        Atlas Pagos · Este es un mensaje automático, no respondas a este correo.
      </p>
    </div>
    """

    templates: dict[str, tuple[str, str]] = {
        "charge_approved": (
            f"✅ Cobro exitoso — {amount_dop}",
            _header + f"""
              <h2 style="color:#16a34a;">¡Pago procesado!</h2>
              <p>Tu pago de <strong>{amount_dop}</strong> para <em>{sub_name}</em>
                 fue procesado exitosamente con la tarjeta terminada en <strong>{card_last4}</strong>.</p>
              <p>Próximo cobro: <strong>{next_date}</strong></p>
              <p>Si no reconoces este cobro, contáctanos de inmediato.</p>
            """ + _footer,
        ),
        "charge_failed": (
            f"⚠️ Cobro fallido — {sub_name}",
            _header + f"""
              <h2 style="color:#dc2626;">Problema con tu pago</h2>
              <p>No pudimos procesar tu cobro de <strong>{amount_dop}</strong>
                 para <em>{sub_name}</em>.</p>
              {"<p><strong>Motivo:</strong> " + fail_reason + "</p>" if fail_reason else ""}
              <p>Intentaremos de nuevo automáticamente. Si el problema persiste,
                 actualiza tu método de pago.</p>
              {"<p>Intentos realizados: <strong>" + str(attempts) + "/3</strong></p>" if attempts else ""}
            """ + _footer,
        ),
        "subscription_paused": (
            f"⏸ Suscripción pausada — {sub_name}",
            _header + f"""
              <h2 style="color:#d97706;">Tu suscripción fue pausada</h2>
              <p>Tu suscripción <em>{sub_name}</em> ha sido pausada temporalmente
                 después de múltiples intentos de cobro fallidos.</p>
              {"<p><strong>Último error:</strong> " + fail_reason + "</p>" if fail_reason else ""}
              <p>Para reactivarla, actualiza tu método de pago o contacta soporte.</p>
            """ + _footer,
        ),
        "subscription_cancelled": (
            f"❌ Suscripción cancelada — {sub_name}",
            _header + f"""
              <h2 style="color:#64748b;">Suscripción cancelada</h2>
              <p>Tu suscripción <em>{sub_name}</em> ha sido cancelada.
                 Tu tarjeta terminada en <strong>{card_last4}</strong> ha sido
                 eliminada de nuestros servidores.</p>
              <p>Esperamos verte de nuevo pronto.</p>
            """ + _footer,
        ),
        "card_expired": (
            f"🔴 Tarjeta vencida — acción requerida",
            _header + f"""
              <h2 style="color:#dc2626;">Tu tarjeta ha vencido</h2>
              <p>La tarjeta terminada en <strong>{card_last4}</strong> asociada a
                 <em>{sub_name}</em> ha vencido.</p>
              <p>Tu suscripción está <strong>pausada</strong> hasta que actualices
                 tu método de pago. No se realizarán cobros hasta entonces.</p>
            """ + _footer,
        ),
        "upcoming_charge": (
            f"📅 Recordatorio de cobro — {amount_dop} el {next_date}",
            _header + f"""
              <h2 style="color:{atlas_blue};">Próximo cobro</h2>
              <p>Te recordamos que el <strong>{next_date}</strong> procesaremos
                 un cobro de <strong>{amount_dop}</strong> para <em>{sub_name}</em>
                 con tu tarjeta terminada en <strong>{card_last4}</strong>.</p>
              <p>Si deseas cancelar antes de esa fecha, puedes hacerlo desde
                 tu cuenta.</p>
            """ + _footer,
        ),
    }

    subject, body = templates.get(event, ("Notificación de Atlas", _header + "<p>Sin contenido</p>" + _footer))
    full_html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
    <body style="background:#f1f5f9;margin:0;padding:32px 0;">
      <div style="max-width:560px;margin:0 auto;">
        {body}
      </div>
    </body>
    </html>
    """
    return subject, full_html


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

async def send_notification(
    event: NotificationEvent,
    to_email: str,
    context: dict,
) -> bool:
    """Send a notification email for a given event.

    Parameters
    ----------
    event:    One of the NotificationEvent literals.
    to_email: Recipient email address.
    context:  Dict with template variables (amount_display, description, etc.)

    Returns True if sent (or logged), False if skipped (empty email).

    Never raises — all errors are caught and logged so that a notification
    failure never blocks a payment transaction.
    """
    if not to_email:
        logger.debug("[notify] Skipped %s — no recipient email.", event)
        return False

    subject, html_body = _render(event, context)

    if not _ENABLED:
        logger.info(
            "[notify] [DEV] Would send '%s' to %s — NOTIFY_ENABLED=0 or FROM_EMAIL not set.\n"
            "Subject: %s",
            event, to_email, subject,
        )
        return True

    try:
        import boto3
        client = boto3.client("ses", region_name=_AWS_REGION)
        client.send_email(
            Source=_FROM_EMAIL,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {
                        "Data": f"{subject}\n\n{context}",
                        "Charset": "UTF-8",
                    },
                },
            },
        )
        logger.info("[notify] Sent '%s' → %s", event, to_email)
        return True

    except Exception as exc:
        logger.error(
            "[notify] Failed to send '%s' → %s: %s",
            event, to_email, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Context builders (helpers for callers)
# ---------------------------------------------------------------------------

def ctx_charge(
    amount: int,
    currency: str,
    description: str,
    card_last4: str,
    next_charge_date: datetime | None = None,
    failure_reason: str = "",
    failed_attempts: int = 0,
) -> dict:
    """Build a notification context dict from raw charge fields."""
    return {
        "amount_display": f"{amount / 100:,.2f}",
        "currency": currency,
        "description": description,
        "card_last4": card_last4,
        "next_charge_date": next_charge_date.strftime("%d/%m/%Y") if next_charge_date else "",
        "failure_reason": failure_reason,
        "failed_attempts": failed_attempts,
    }
