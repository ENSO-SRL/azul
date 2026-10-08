"""
Registration — Trial de 30 días sin tarjeta.

POST /api/v1/registration/trial
    Recibe los datos de registro del usuario desde el frontend y crea
    automáticamente una suscripción con trial de 30 días sin necesidad
    de tarjeta de crédito.

    El email se resuelve contra public.users y se guarda el ID del usuario.
    Las suscripciones antiguas por email se reutilizan sin reiniciar el trial.

    Idempotente: si el usuario ya tiene una suscripción ACTIVE,
    retorna 200 con los datos del trial existente en lugar de crear uno nuevo.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_db
from app.domain.entities import RecurringPayment
from app.infrastructure.repo_impl import SQLRecurringRepository
from app.services.subscription_identity import (
    CustomerIdentityError,
    find_active_subscription,
    lock_customer_subscriptions,
    resolve_customer_identity,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/registration", tags=["Registration"])

# Trial de 30 días para usuarios que se registran sin tarjeta
REGISTRATION_TRIAL_DAYS = int(os.getenv("REGISTRATION_TRIAL_DAYS", "30"))

# Monto de membresía — el scheduler usará esto cuando expire el trial
MEMBERSHIP_AMOUNT = int(os.getenv("MEMBERSHIP_AMOUNT", "50000"))   # RD$500.00
MEMBERSHIP_ITBIS  = int(os.getenv("MEMBERSHIP_ITBIS",  "9000"))    # RD$90.00


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RegistrationRequest(BaseModel):
    """Payload que envía el frontend al registrar un usuario nuevo."""
    email: str = Field(..., min_length=5, max_length=255)
    name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    phone: str = Field("", max_length=30)
    gender: str = Field("", max_length=20)
    # password y confirmPassword los acepta el modelo para compatibilidad
    # con el payload del frontend, pero NO se almacenan aquí.
    password: str = Field("", exclude=True)
    confirmPassword: str = Field("", exclude=True)

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Email inválido")
        return v


class TrialRegistrationResponse(BaseModel):
    """Respuesta del endpoint de registro con trial."""
    status: str                   # "created" | "already_active"
    customer_id: str              # ID canónico del usuario en Atlas
    trial_ends_at: str            # ISO 8601 UTC
    next_charge_at: str           # igual a trial_ends_at
    trial_days: int
    message: str


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post(
    "/trial",
    response_model=TrialRegistrationResponse,
    status_code=201,
    summary="Registrar usuario con trial de 30 días (sin tarjeta)",
    description=(
        "Crea una suscripción en período de prueba de 30 días para un usuario "
        "recién registrado. No requiere tarjeta de crédito. "
        "El status del usuario aparecerá como `trial_no_card` en "
        "`GET /api/v1/tokens/status/{customer_id}`. "
        "Al vencer el trial sin tarjeta, el status cambia a `trial_expired_no_card`. "
        "**Idempotente**: si el usuario ya tiene una suscripción activa retorna 200."
    ),
)
async def register_trial(
    body: RegistrationRequest,
    db: AsyncSession = Depends(get_db),
):
    """Crea trial de 30 días para usuario nuevo sin tarjeta.

    Requiere que el usuario ya exista en Atlas. El correo del payload se
    resuelve al ID canónico; no es necesario cambiar el payload del frontend.
    """
    try:
        identity = await resolve_customer_identity(db, body.email)
        await lock_customer_subscriptions(db, identity)
        existing_sub = await find_active_subscription(db, identity, for_update=True)
        from app.services.payment_lifecycle import assert_no_pending_membership, subscription_history
        if existing_sub is None:
            await assert_no_pending_membership(db, identity)
            if await subscription_history(db, identity):
                raise CustomerIdentityError('Esta cuenta ya tuvo una suscripción; no corresponde otra prueba gratuita.')
    except CustomerIdentityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[registration] No se pudo verificar la identidad del usuario")
        raise HTTPException(status_code=503, detail="No se pudo verificar la cuenta. Intenta de nuevo.") from exc
    customer_id = identity.customer_id
    full_name = f"{body.name.strip()} {body.last_name.strip()}".strip()

    logger.warning(
        "[registration] ▶ POST /trial | customer_id=%s name=%s",
        customer_id, full_name,
    )

    now = datetime.now(timezone.utc)

    # ── Guard: idempotencia — ¿ya tiene suscripción ACTIVE? ──────────────
    if existing_sub:
        existing_sub.customer_id = customer_id
        await db.commit()
        trial_ends = existing_sub.trial_ends_at
        next_charge = existing_sub.next_charge_at
        logger.warning(
            "[registration] ⏭ usuario ya tiene suscripción ACTIVE — no se crea duplicado | customer_id=%s",
            customer_id,
        )
        return TrialRegistrationResponse(
            status="already_active",
            customer_id=customer_id,
            trial_ends_at=trial_ends.isoformat() if trial_ends else "",
            next_charge_at=next_charge.isoformat() if next_charge else "",
            trial_days=REGISTRATION_TRIAL_DAYS,
            message=(
                f"El usuario ya tiene un período de prueba activo. "
                f"Vence el {trial_ends.strftime('%d/%m/%Y') if trial_ends else 'N/A'}."
            ),
        )

    # ── Crear suscripción trial sin tarjeta ───────────────────────────────
    trial_end = now + timedelta(days=REGISTRATION_TRIAL_DAYS)

    recurring = RecurringPayment(
        customer_id=customer_id,
        amount=MEMBERSHIP_AMOUNT,
        itbis=MEMBERSHIP_ITBIS,
        frequency_days=30,
        description="Membresía Atlas — Período de prueba",
        # Sin tarjeta — el scheduler NO cobrará porque data_vault_token está vacío
        data_vault_token="",
        card_brand="",
        card_last4="",
        card_expiration="",
        cardholder_email=identity.email,
        next_charge_at=trial_end,
        last_charged_at=None,
        trial_ends_at=trial_end,
    )

    try:
        repo = SQLRecurringRepository(db)
        await repo.save(recurring)
        logger.warning(
            "[registration] ✓ trial creado | customer_id=%s trial_ends=%s",
            customer_id, trial_end.isoformat(),
        )
    except Exception as exc:
        await db.rollback()
        logger.error(
            "[registration] ✗ error creando trial | customer_id=%s err=%s",
            customer_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Error al crear período de prueba: {exc}",
        )

    return TrialRegistrationResponse(
        status="created",
        customer_id=customer_id,
        trial_ends_at=trial_end.isoformat(),
        next_charge_at=trial_end.isoformat(),
        trial_days=REGISTRATION_TRIAL_DAYS,
        message=(
            f"Período de prueba de {REGISTRATION_TRIAL_DAYS} días creado exitosamente. "
            f"Primer cobro programado para el {trial_end.strftime('%d/%m/%Y')} "
            f"si se registra una tarjeta antes de esa fecha."
        ),
    )
