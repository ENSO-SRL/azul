"""
Token endpoints — DataVault card management.

POST   /api/v1/tokens                      → Register a card (TrxType CREATE, no charge)
GET    /api/v1/tokens/by-email/{email}      → Safe card lookup by email (for frontend visual)
GET    /api/v1/tokens/{customer_id}         → List saved cards for a customer (full data)
DELETE /api/v1/tokens/{token}               → Remove a card from DataVault + local DB
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.azul_gateway import AzulIntegrationError, AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.token_service import TokenService

router = APIRouter(prefix="/api/v1/tokens", tags=["Tokens / DataVault"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RegisterCardRequest(BaseModel):
    customer_id: str = Field(..., description="ID del cliente en Atlas")
    card_number: str = Field(..., description="Número de tarjeta (16-19 dígitos)")
    expiration: str  = Field(..., description="Expiración YYYYMM (ej. 202812)")
    cvc: str         = Field(..., description="CVC / CVV")
    cardholder_name: str  = Field(..., description="Nombre del tarjetahabiente")
    cardholder_email: str = Field(..., description="Correo electrónico del tarjetahabiente")

    model_config = {"json_schema_extra": {"examples": [
        {
            "customer_id": "usr_12345",
            "card_number": "4260550061845872",
            "expiration": "202812",
            "cvc": "123",
            "cardholder_name": "Juan Pérez",
            "cardholder_email": "juan@ejemplo.com",
        }
    ]}}


class SavedCardResponse(BaseModel):
    id: str
    customer_id: str
    token: str
    card_brand: str
    card_last4: str
    expiration: str
    is_default: bool
    created_at: str


class SavedCardSafeResponse(BaseModel):
    """Datos seguros de tarjeta para renderizar en el visual del frontend.

    NO expone el token DataVault completo — solo un fragmento enmascarado.
    """
    id: str
    card_brand: str
    card_last4: str
    expiration: str
    expiration_display: str  # MM/AA format for visual
    is_default: bool
    token_masked: str        # ej. "129B****802C"
    created_at: str


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def _get_service(db: AsyncSession = Depends(get_db)) -> TokenService:
    return TokenService(
        card_repo=SQLSavedCardRepository(db),
        gateway=AzulPaymentGateway(),
    )


# ---------------------------------------------------------------------------
# Routes
# NOTA: /status/{customer_id} y /by-email/{email} deben ir ANTES de
# /{customer_id} para que FastAPI no interprete esos prefijos como customer_id.
# ---------------------------------------------------------------------------

@router.get(
    "/status/{customer_id}",
    summary="Estado de pago del usuario",
    description=(
        "Devuelve un resumen completo del estado de pago de un usuario: "
        "tarjetas guardadas, suscripciones activas/pausadas, últimos pagos, "
        "y un indicador de si el usuario está al día o tiene problemas de pago."
    ),
)
async def get_user_payment_status(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Estado integral de pago del usuario.

    Cruza datos de:
    - pagos.saved_cards → tarjetas guardadas
    - pagos.recurring_payments → suscripciones
    - pagos.payments → historial de pagos recientes
    - public.users → datos del usuario (nombre, email)

    Retorna un JSON con:
    - user_info: datos básicos del usuario
    - cards: lista de tarjetas guardadas con estado de vencimiento
    - subscriptions: lista de suscripciones con estado y reintentos
    - recent_payments: últimos 10 pagos
    - summary: indicador resumen (active, payment_issues, no_card, etc.)
    """
    from sqlalchemy import text, select
    from datetime import datetime, timezone
    from calendar import monthrange
    from app.infrastructure.models import (
        SavedCardModel,
        RecurringPaymentModel,
        PaymentModel,
    )

    now = datetime.now(timezone.utc)

    # ── 1. Datos del usuario desde public.users ──────────────────────────
    user_info = {"customer_id": customer_id, "email": "", "name": "", "found": False}
    try:
        result = await db.execute(
            text(
                "SELECT email, name, last_name FROM public.users "
                "WHERE uuid::text = :cid OR email = :cid LIMIT 1"
            ),
            {"cid": customer_id},
        )
        row = result.fetchone()
        if row:
            user_info["email"] = row[0] or ""
            user_info["name"] = f"{row[1] or ''} {row[2] or ''}".strip()
            user_info["found"] = True
    except Exception:
        pass  # public.users might not be accessible — continue with pagos data

    # ── 2. Tarjetas guardadas ────────────────────────────────────────────
    cards_result = await db.execute(
        select(SavedCardModel)
        .where(SavedCardModel.customer_id == customer_id)
        .order_by(SavedCardModel.created_at.desc())
    )
    cards_raw = cards_result.scalars().all()

    # Deduplicate by token — legacy bug could have created duplicates
    seen_tokens: set[str] = set()
    cards_deduped = []
    for c in cards_raw:
        if c.token not in seen_tokens:
            seen_tokens.add(c.token)
            cards_deduped.append(c)

    cards = []
    for c in cards_deduped:
        # Verificar si la tarjeta está vencida
        expired = False
        if c.expiration and len(c.expiration) == 6:
            try:
                exp_y = int(c.expiration[:4])
                exp_m = int(c.expiration[4:6])
                last_day = monthrange(exp_y, exp_m)[1]
                exp_date = datetime(exp_y, exp_m, last_day, 23, 59, 59, tzinfo=timezone.utc)
                expired = now > exp_date
            except (ValueError, IndexError):
                pass

        cards.append({
            "id": c.id,
            "card_brand": c.card_brand or "",
            "card_last4": c.card_last4 or "",
            "expiration": c.expiration or "",
            "expiration_display": (
                f"{c.expiration[4:]}/{c.expiration[2:4]}"
                if c.expiration and len(c.expiration) == 6
                else ""
            ),
            "is_default": c.is_default,
            "is_expired": expired,
            "status": "expired" if expired else "active",
            "created_at": c.created_at.isoformat() if c.created_at else "",
        })

    # ── 3. Suscripciones ─────────────────────────────────────────────────
    subs_result = await db.execute(
        select(RecurringPaymentModel)
        .where(RecurringPaymentModel.customer_id == customer_id)
        .order_by(RecurringPaymentModel.created_at.desc())
    )
    subs_raw = subs_result.scalars().all()

    subscriptions = []
    for s in subs_raw:
        # Trial detection
        sub_trial_ends = getattr(s, "trial_ends_at", None)
        sub_in_trial = bool(sub_trial_ends and sub_trial_ends > now and s.status == "ACTIVE")

        subscriptions.append({
            "id": s.id,
            "status": s.status,
            "amount": s.amount,
            "amount_display": f"RD${s.amount / 100:,.2f}",
            "frequency_days": s.frequency_days,
            "description": s.description or "",
            "card_last4": s.card_last4 or "",
            "card_brand": s.card_brand or "",
            "next_charge_at": s.next_charge_at.isoformat() if s.next_charge_at else None,
            "last_charged_at": s.last_charged_at.isoformat() if s.last_charged_at else None,
            "failed_attempts": s.failed_attempts,
            "last_failure_reason": s.last_failure_reason or "",
            "in_trial": sub_in_trial,
            "trial_ends_at": sub_trial_ends.isoformat() if sub_trial_ends else None,
            "created_at": s.created_at.isoformat() if s.created_at else "",
        })

    # ── 4. Últimos 10 pagos ──────────────────────────────────────────────
    payments_result = await db.execute(
        select(PaymentModel)
        .where(PaymentModel.customer_id == customer_id)
        .order_by(PaymentModel.created_at.desc())
        .limit(10)
    )
    payments_raw = payments_result.scalars().all()

    recent_payments = []
    for p in payments_raw:
        recent_payments.append({
            "id": p.id,
            "status": p.status,
            "amount": p.amount,
            "amount_display": f"RD${p.amount / 100:,.2f}",
            "iso_code": p.iso_code or "",
            "response_message": p.response_message or "",
            "card_last4": p.card_number_masked[-4:] if p.card_number_masked else "",
            "payment_type": p.payment_type or "",
            "created_at": p.created_at.isoformat() if p.created_at else "",
        })

    # ── 5. Calcular resumen ──────────────────────────────────────────────
    has_cards = len(cards) > 0
    has_active_card = any(c["status"] == "active" for c in cards)
    active_subs = [s for s in subscriptions if s["status"] == "ACTIVE"]
    paused_subs = [s for s in subscriptions if s["status"] == "PAUSED"]
    failing_subs = [s for s in subscriptions if int(s.get("failed_attempts") or 0) > 0]
    trial_subs = [s for s in subscriptions if s.get("in_trial")]

    if not has_cards:
        overall_status = "no_card"
        status_message = "El usuario no tiene tarjetas guardadas."
    elif not has_active_card:
        overall_status = "card_expired"
        status_message = "Todas las tarjetas del usuario están vencidas."
    elif trial_subs:
        overall_status = "trial"
        trial_end = trial_subs[0].get("trial_ends_at", "")
        status_message = f"Período de gracia activo. Primer cobro programado para {trial_end}."
    elif paused_subs:
        overall_status = "payment_issues"
        reasons = set(s["last_failure_reason"] for s in paused_subs if s["last_failure_reason"])
        status_message = (
            f"{len(paused_subs)} suscripción(es) pausada(s) por fallos de cobro. "
            f"Razón(es): {', '.join(str(r) for r in reasons) if reasons else 'desconocida'}."
        )
    elif failing_subs:
        overall_status = "retrying"
        status_message = (
            f"{len(failing_subs)} suscripción(es) con reintentos pendientes. "
            f"Próximo intento más cercano: "
            f"{min(s['next_charge_at'] for s in failing_subs if s['next_charge_at']) or 'N/A'}."
        )
    elif active_subs:
        overall_status = "active"
        status_message = f"Todo al día. {len(active_subs)} suscripción(es) activa(s)."
    else:
        overall_status = "no_subscription"
        status_message = "El usuario tiene tarjeta(s) pero no tiene suscripciones activas."

    return {
        "user_info": user_info,
        "cards": cards,
        "cards_count": len(cards),
        "subscriptions": subscriptions,
        "subscriptions_count": len(subscriptions),
        "recent_payments": recent_payments,
        "summary": {
            "status": overall_status,
            "message": status_message,
            "has_cards": has_cards,
            "has_active_card": has_active_card,
            "active_subscriptions": len(active_subs),
            "paused_subscriptions": len(paused_subs),
            "failing_subscriptions": len(failing_subs),
            "in_trial": len(trial_subs) > 0,
            "trial_ends_at": trial_subs[0].get("trial_ends_at") if trial_subs else None,
        },
    }

@router.post(
    "",
    response_model=SavedCardResponse,
    status_code=201,
    summary="Registrar tarjeta en DataVault (sin cobrar)",
)
async def register_card(
    body: RegisterCardRequest,
    svc: TokenService = Depends(_get_service),
):
    """Store a card in Azul DataVault without charging it.

    Use this endpoint during onboarding so the customer enters their card
    once and subsequent charges use the returned ``token``.
    """
    try:
        card = await svc.register_card(
            customer_id=body.customer_id,
            card_number=body.card_number,
            expiration=body.expiration,
            cvc=body.cvc,
            cardholder_name=body.cardholder_name,
            cardholder_email=body.cardholder_email,
        )
    except ValueError as exc:
        err = str(exc)
        if "VALIDATION_ERROR:TrxType" in err:
            raise HTTPException(
                status_code=503,
                detail=(
                    "DataVault CREATE (tokenizar sin cobrar) no está habilitado en sandbox. "
                    "Usa POST /api/v1/payments con save_card=true para obtener un token "
                    "en el mismo cobro. Solicitar habilitación a solucionesintegradas@bpd.com.do."
                ),
            )
        raise HTTPException(status_code=422, detail=err)
    return _to_response(card)


@router.get(
    "/by-email/{email}",
    response_model=list[SavedCardSafeResponse],
    summary="Consultar tarjetas guardadas por correo electrónico",
    description=(
        "Devuelve las tarjetas guardadas de un usuario dado su correo electrónico. "
        "Solo retorna datos seguros para renderizar en el frontend: últimos 4 dígitos, "
        "marca, expiración y un token enmascarado. NUNCA expone el token DataVault completo."
    ),
)
async def get_cards_by_email(
    email: str,
    svc: TokenService = Depends(_get_service),
):
    """Busca tarjetas guardadas por email del usuario.

    El email es el customer_id con el que se tokenizaron las tarjetas
    durante el flujo de pago o registro.
    """
    cards = await svc.list_cards(customer_id=email)
    if not cards:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron tarjetas guardadas para el correo '{email}'.",
        )
    return [_to_safe_response(c) for c in cards]


@router.get(
    "/{customer_id}",
    response_model=list[SavedCardResponse],
    summary="Listar tarjetas guardadas de un cliente",
)
async def list_cards(
    customer_id: str,
    svc: TokenService = Depends(_get_service),
):
    cards = await svc.list_cards(customer_id)
    return [_to_response(c) for c in cards]


@router.delete(
    "/{token}",
    status_code=204,
    summary="Eliminar tarjeta de DataVault (por token)",
)
async def delete_card(
    token: str,
    customer_id: str,
    svc: TokenService = Depends(_get_service),
):
    """Remove a card from DataVault and local DB.

    ``customer_id`` must match the card owner — this prevents cross-customer deletion.
    """
    try:
        await svc.delete_card(customer_id=customer_id, token=token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except AzulIntegrationError as exc:
        raise HTTPException(status_code=503, detail=f"Error al eliminar tarjeta en Azul: {exc}")


@router.delete(
    "/by-email/{email}/{card_id}",
    status_code=204,
    summary="Eliminar tarjeta por ID y correo electrónico",
    description=(
        "Elimina una tarjeta guardada usando su ID (UUID) y el correo del dueño. "
        "No necesitas el token DataVault — usa el 'id' que devuelve GET /by-email/{email}."
    ),
)
async def delete_card_by_id(
    email: str,
    card_id: str,
    svc: TokenService = Depends(_get_service),
):
    """Elimina una tarjeta por su ID de base de datos.

    El email verifica que la tarjeta pertenece al usuario.
    El card_id es el 'id' que devuelve el endpoint GET /by-email/{email}.
    """
    try:
        await svc.delete_card_by_id(card_id=card_id, customer_email=email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except AzulIntegrationError as exc:
        raise HTTPException(status_code=503, detail=f"Error al eliminar tarjeta en Azul: {exc}")


@router.put(
    "/{customer_id}/default/{card_id}",
    status_code=200,
    summary="Marcar una tarjeta como predeterminada",
)
async def set_default_card(
    customer_id: str,
    card_id: str,
    svc: TokenService = Depends(_get_service),
):
    """Marca una tarjeta específica como la predeterminada de cobro para el cliente."""
    try:
        await svc.set_default_card(customer_id=customer_id, card_id=card_id)
        return {"message": "Tarjeta predeterminada actualizada exitosamente."}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_response(c) -> dict:
    return {
        "id": c.id,
        "customer_id": c.customer_id,
        "token": c.token,
        "card_brand": c.card_brand,
        "card_last4": c.card_last4,
        "expiration": c.expiration,
        "is_default": c.is_default,
        "created_at": c.created_at.isoformat(),
    }


def _mask_token(token: str) -> str:
    """Enmascara el token DataVault: muestra primeros 4 y últimos 4 caracteres."""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}****{token[-4:]}"


def _format_expiration_display(expiration: str) -> str:
    """Convierte YYYYMM → MM/AA para mostrar en el visual."""
    if len(expiration) == 6:
        return f"{expiration[4:]}/{expiration[2:4]}"
    return expiration


def _to_safe_response(c) -> dict:
    """Construye respuesta segura sin token completo — para renderizar en el visual."""
    return {
        "id": c.id,
        "card_brand": c.card_brand,
        "card_last4": c.card_last4,
        "expiration": c.expiration,
        "expiration_display": _format_expiration_display(c.expiration),
        "is_default": c.is_default,
        "token_masked": _mask_token(c.token),
        "created_at": c.created_at.isoformat(),
    }
