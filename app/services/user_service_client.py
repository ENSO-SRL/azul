"""
Cliente HTTP saliente -> Atlas User Service.

Responsabilidad unica: enviar el resultado de un cobro de suscripcion al
endpoint /invoices/service/payment_email del user service, para que este
despache el correo transaccional al cliente.

Configuracion (variables de entorno / secrets ECS)
---------------------------------------------------
ATLAS_API_BASE_URL   URL base del user service (ej. https://...ecs.us-east-2.on.aws/)
USER_SERVICE_KEY     Valor del header x-service-key

Modo dev (sin env vars)
-----------------------
Si alguna de las dos variables falta, la funcion loguea una advertencia y
retorna True sin hacer ninguna llamada HTTP. Esto permite correr en local sin
credenciales de produccion.

Flag de corte de emergencia
---------------------------
PAYMENT_EMAIL_ENABLED  "0" deshabilita todas las llamadas sin redeploy de codigo.
Default: "1" (activo).

Comportamiento de errores
--------------------------
La funcion NUNCA lanza. Captura cualquier excepcion, la loguea (nivel WARNING
o ERROR segun el tipo), y devuelve False. Asi un fallo de correo no puede
revertir ni bloquear el cobro en el scheduler.

Mapeo de status HTTP -> log
--------------------------
  200       -> True  (correo enviado)
  401       -> ERROR (key mal configurada -- bug de integracion)
  422       -> ERROR (payload invalido -- bug de integracion)
  502       -> WARNING (proveedor de correo fallo -- transitorio)
  otro 4xx  -> WARNING
  otro 5xx  -> ERROR
  timeout   -> WARNING
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_TIMEOUT_SECONDS = 10.0


def _get_config() -> tuple[str, str] | None:
    """Devuelve (base_url, service_key) o None si faltan vars."""
    base_url = os.getenv("ATLAS_API_BASE_URL", "").rstrip("/")
    service_key = os.getenv("USER_SERVICE_KEY", "")
    if not base_url or not service_key:
        return None
    return base_url, service_key


def _is_enabled() -> bool:
    return os.getenv("PAYMENT_EMAIL_ENABLED", "1") == "1"


# ---------------------------------------------------------------------------
# Funcion publica
# ---------------------------------------------------------------------------


async def enviar_correo_pago(
    *,
    to_email: str,
    success: bool,
    name: str | None = None,
    invoice_number: str | None = None,
    total: float | None = None,
    currency: str = "DOP",
    payment_method: str | None = None,
    establishment_name: str | None = None,
    failure_reason: str | None = None,
    invoice_items: list[dict[str, Any]] | None = None,
) -> bool:
    """Notifica al user service el resultado de un cobro de suscripcion.

    Parameters
    ----------
    to_email:          Email del titular de la suscripcion.
    success:           True -> cobro aprobado; False -> cobro declinado.
    name:              Nombre del titular (opcional, default "cliente").
    invoice_number:    Numero de factura. Obligatorio cuando success=True.
    total:             Monto cobrado en la unidad de la moneda (no centavos).
                       Obligatorio cuando success=True.
    currency:          Codigo de moneda (default "DOP").
    payment_method:    Descripcion del metodo (ej. "Tarjeta terminada en 4242").
    establishment_name: Nombre del establecimiento asociado.
    failure_reason:    Motivo del fallo. Solo relevante cuando success=False.
    invoice_items:     Lista de dicts [{description, amount}] para tabla de factura.

    Returns
    -------
    True si el user service respondio 200, False en cualquier otro caso.
    Nunca lanza excepciones.
    """
    if not _is_enabled():
        logger.info(
            "[user_svc] PAYMENT_EMAIL_ENABLED=0 -- correo omitido para %s (success=%s).",
            to_email, success,
        )
        return True

    cfg = _get_config()
    if cfg is None:
        logger.warning(
            "[user_svc] ATLAS_API_BASE_URL o USER_SERVICE_KEY no configurados -- "
            "modo dev, correo omitido para %s.", to_email,
        )
        return True  # no falla el cobro por falta de config local

    base_url, service_key = cfg
    endpoint = f"{base_url}/invoices/service/payment_email"

    # ------------------------------------------------------------------
    # Construir payload (solo campos con valor)
    # ------------------------------------------------------------------
    payload: dict[str, Any] = {
        "email": to_email,
        "success": success,
        "currency": currency,
    }
    if name:
        payload["name"] = name
    if invoice_number:
        payload["invoice_number"] = invoice_number
    if total is not None:
        payload["total"] = total
    if payment_method:
        payload["payment_method"] = payment_method
    if establishment_name:
        payload["establishment_name"] = establishment_name
    if failure_reason:
        payload["failure_reason"] = failure_reason
    if invoice_items:
        payload["invoice_data"] = {"items": invoice_items}

    # ------------------------------------------------------------------
    # Llamada HTTP
    # ------------------------------------------------------------------
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={
                    "x-service-key": service_key,
                    "Content-Type": "application/json",
                },
            )

        status = response.status_code

        if status == 200:
            logger.info(
                "[user_svc] Correo de pago enviado OK -> %s (success=%s).",
                to_email, success,
            )
            return True

        elif status == 401:
            logger.error(
                "[user_svc] 401 Unauthorized -- USER_SERVICE_KEY invalido o expirado. "
                "Verificar credencial en SSM. to_email=%s", to_email,
            )
        elif status == 422:
            logger.error(
                "[user_svc] 422 Unprocessable -- payload rechazado. Revisar campos "
                "invoice_number/total cuando success=True. "
                "to_email=%s payload=%s respuesta=%s",
                to_email, payload, response.text,
            )
        elif status == 502:
            logger.warning(
                "[user_svc] 502 -- proveedor de correo fallo (transitorio). "
                "to_email=%s", to_email,
            )
        elif 400 <= status < 500:
            logger.warning(
                "[user_svc] %d del user service. to_email=%s respuesta=%s",
                status, to_email, response.text,
            )
        else:
            logger.error(
                "[user_svc] %d del user service. to_email=%s respuesta=%s",
                status, to_email, response.text,
            )

        return False

    except httpx.TimeoutException:
        logger.warning(
            "[user_svc] Timeout (%ss) al llamar %s para %s.",
            _TIMEOUT_SECONDS, endpoint, to_email,
        )
        return False

    except httpx.RequestError as exc:
        logger.warning(
            "[user_svc] Error de red al llamar %s: %s", endpoint, exc,
        )
        return False

    except Exception as exc:
        logger.error(
            "[user_svc] Error inesperado enviando correo a %s: %s", to_email, exc,
        )
        return False
