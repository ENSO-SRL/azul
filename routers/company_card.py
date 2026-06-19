"""
Company Card (Tarjeta de Recibo) — endpoint interno.

PUT    /api/v1/company-card   → Guardar / reemplazar la tarjeta de la empresa
GET    /api/v1/company-card   → Recuperar datos en texto plano (auto-fill HTML)
DELETE /api/v1/company-card   → Eliminar la tarjeta

Uso: la empresa registra UNA tarjeta general que la automatización consume
directamente para rellenar los campos del formulario de pago.

Seguridad: protegido con X-API-Key. Datos encriptados at-rest con Fernet.
"""

from __future__ import annotations

import logging
import os
from base64 import urlsafe_b64encode
from datetime import datetime, timezone
from hashlib import sha256

from cryptography.fernet import Fernet  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, String, Text, select, delete
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import Base, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/company-card", tags=["Tarjeta de Empresa"])


# ---------------------------------------------------------------------------
# Encryption — Fernet (AES-128-CBC) con clave de entorno
# ---------------------------------------------------------------------------

def _get_fernet() -> Fernet:
    """Devuelve instancia Fernet. Genera clave derivada de COMPANY_CARD_SECRET."""
    secret = os.getenv("COMPANY_CARD_SECRET", "")
    if not secret:
        raise RuntimeError(
            "COMPANY_CARD_SECRET no está configurada. "
            "Genera una con: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    # Si el secret ya es una clave Fernet válida (44 chars base64), usarla directo.
    # Si no, derivar una clave de 32 bytes con SHA-256 y codificarla en base64.
    try:
        return Fernet(secret.encode())
    except Exception:
        key = urlsafe_b64encode(sha256(secret.encode()).digest())
        return Fernet(key)


def _encrypt(text: str) -> str:
    """Encripta texto plano → string encriptado."""
    return _get_fernet().encrypt(text.encode()).decode()


def _decrypt(token: str) -> str:
    """Desencripta string encriptado → texto plano."""
    return _get_fernet().decrypt(token.encode()).decode()


# ---------------------------------------------------------------------------
# ORM Model — tabla tarjeta_empresa (schema public)
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TarjetaEmpresaModel(Base):
    """Tarjeta de recibo/cobro de la empresa. Solo una fila activa."""

    __tablename__ = "tarjeta_empresa"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    card_number_enc: Mapped[str] = mapped_column(Text, nullable=False)
    expiration: Mapped[str] = mapped_column(String(6), nullable=False)
    cvc_enc: Mapped[str] = mapped_column(Text, nullable=False)
    cardholder_name: Mapped[str] = mapped_column(String(100), nullable=False)
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    card_brand: Mapped[str] = mapped_column(String(20), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ---------------------------------------------------------------------------
# Schemas (request / response)
# ---------------------------------------------------------------------------

class CompanyCardRequest(BaseModel):
    """Datos de la tarjeta de la empresa."""
    card_number: str = Field(..., min_length=13, max_length=19,
                             description="Número de tarjeta completo (13-19 dígitos)")
    expiration: str = Field(..., min_length=6, max_length=6,
                            description="Fecha de expiración YYYYMM (ej. 202812)")
    cvc: str = Field(..., min_length=3, max_length=4,
                     description="Código de seguridad (3-4 dígitos)")
    cardholder_name: str = Field(..., min_length=2, max_length=100,
                                 description="Nombre del titular de la tarjeta")
    description: str = Field("", max_length=255,
                             description="Etiqueta opcional (ej. 'Tarjeta principal')")

    model_config = {"json_schema_extra": {"examples": [
        {
            "card_number": "4260550061845872",
            "expiration": "202812",
            "cvc": "123",
            "cardholder_name": "COLINA DEL SOL SRL",
            "description": "Tarjeta principal de cobro",
        }
    ]}}


class CompanyCardResponse(BaseModel):
    """Datos de la tarjeta en texto plano — para auto-fill del HTML."""
    id: int
    card_number: str
    expiration: str
    cvc: str
    cardholder_name: str
    card_last4: str
    card_brand: str
    description: str
    is_active: bool
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_brand(card_number: str) -> str:
    """Detecta la marca de la tarjeta por el BIN (primeros dígitos)."""
    if card_number.startswith("4"):
        return "VISA"
    elif card_number[:2] in ("51", "52", "53", "54", "55") or (
        2221 <= int(card_number[:4]) <= 2720 if len(card_number) >= 4 else False
    ):
        return "MASTERCARD"
    elif card_number[:2] in ("34", "37"):
        return "AMEX"
    elif card_number[:4] in ("6011", "6221", "6229") or card_number[:2] == "65":
        return "DISCOVER"
    return "UNKNOWN"


def _luhn_check(card_number: str) -> bool:
    """Validación Luhn del número de tarjeta."""
    digits = [int(d) for d in card_number]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _validate_card(body: CompanyCardRequest) -> None:
    """Valida los datos de la tarjeta antes de guardar."""
    # Solo dígitos en card_number
    if not body.card_number.isdigit():
        raise HTTPException(status_code=422, detail="card_number debe contener solo dígitos")

    # Luhn check
    if not _luhn_check(body.card_number):
        raise HTTPException(status_code=422, detail="Número de tarjeta inválido (falla validación Luhn)")

    # Expiración: YYYYMM y no vencida
    if not body.expiration.isdigit() or len(body.expiration) != 6:
        raise HTTPException(status_code=422, detail="expiration debe ser YYYYMM (ej. 202812)")

    year = int(body.expiration[:4])
    month = int(body.expiration[4:6])
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="Mes de expiración inválido (01-12)")

    now = datetime.now(timezone.utc)
    if year < now.year or (year == now.year and month < now.month):
        raise HTTPException(status_code=422, detail="La tarjeta está vencida")

    # CVC solo dígitos
    if not body.cvc.isdigit():
        raise HTTPException(status_code=422, detail="cvc debe contener solo dígitos")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.put(
    "",
    response_model=CompanyCardResponse,
    summary="Guardar / reemplazar la tarjeta de la empresa",
    description=(
        "Registra la tarjeta de recibo de la empresa. Si ya existe una tarjeta, "
        "la reemplaza (solo puede haber una activa). Los datos sensibles se "
        "encriptan en la base de datos."
    ),
)
async def save_company_card(
    body: CompanyCardRequest,
    db: AsyncSession = Depends(get_db),
):
    print("\n" + "="*60)
    print("📥 [COMPANY-CARD] PUT — Guardar tarjeta de empresa")
    print(f"   Titular: {body.cardholder_name}")
    print(f"   Tarjeta: ****{body.card_number[-4:]}")
    print(f"   Expiración: {body.expiration}")
    print(f"   Descripción: {body.description or '(sin descripción)'}")
    print("="*60)

    _validate_card(body)
    print("✅ Validación Luhn + expiración OK")

    card_last4 = body.card_number[-4:]
    card_brand = _detect_brand(body.card_number)
    print(f"🏷️  Marca detectada: {card_brand}")

    # Encriptar datos sensibles
    card_number_enc = _encrypt(body.card_number)
    cvc_enc = _encrypt(body.cvc)
    print(f"🔐 Datos encriptados (card_number_enc={card_number_enc[:20]}..., cvc_enc={cvc_enc[:20]}...)")

    # Buscar tarjeta existente (solo puede haber una)
    result = await db.execute(
        select(TarjetaEmpresaModel).where(TarjetaEmpresaModel.is_active == True)
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Actualizar la existente
        print(f"🔄 Tarjeta existente encontrada (id={existing.id}, last4={existing.card_last4}) → ACTUALIZANDO")
        existing.card_number_enc = card_number_enc
        existing.expiration = body.expiration
        existing.cvc_enc = cvc_enc
        existing.cardholder_name = body.cardholder_name
        existing.card_last4 = card_last4
        existing.card_brand = card_brand
        existing.description = body.description
        existing.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing)
        model = existing
        print(f"✅ Tarjeta ACTUALIZADA en DB (id={model.id})")
        logger.warning(f"[company-card] Tarjeta ACTUALIZADA (id={model.id}, last4={card_last4}, brand={card_brand})")
    else:
        # Crear nueva
        print("🆕 No hay tarjeta existente → CREANDO nueva")
        model = TarjetaEmpresaModel(
            card_number_enc=card_number_enc,
            expiration=body.expiration,
            cvc_enc=cvc_enc,
            cardholder_name=body.cardholder_name,
            card_last4=card_last4,
            card_brand=card_brand,
            description=body.description,
        )
        db.add(model)
        await db.commit()
        await db.refresh(model)
        print(f"✅ Tarjeta CREADA en DB (id={model.id})")
        logger.warning(f"[company-card] Tarjeta CREADA (id={model.id}, last4={card_last4}, brand={card_brand})")

    response = _to_response(model)
    print(f"📤 Respuesta: id={response['id']}, brand={response['card_brand']}, last4={response['card_last4']}")
    print("="*60 + "\n")
    return response


@router.get(
    "",
    response_model=CompanyCardResponse,
    summary="Recuperar tarjeta de la empresa (texto plano)",
    description=(
        "Devuelve los datos completos de la tarjeta de la empresa en texto plano. "
        "Diseñado para que la automatización consuma estos datos directamente "
        "y los coloque en los campos del formulario HTML de pago."
    ),
)
async def get_company_card(
    db: AsyncSession = Depends(get_db),
):
    print("\n" + "="*60)
    print("📤 [COMPANY-CARD] GET — Recuperar tarjeta de empresa")
    print("="*60)

    result = await db.execute(
        select(TarjetaEmpresaModel).where(TarjetaEmpresaModel.is_active == True)
    )
    model = result.scalar_one_or_none()

    if not model:
        print("❌ No hay tarjeta registrada → 404")
        print("="*60 + "\n")
        raise HTTPException(
            status_code=404,
            detail="No hay tarjeta de empresa registrada. Usa PUT /api/v1/company-card para crear una.",
        )

    response = _to_response(model)
    print(f"✅ Tarjeta encontrada:")
    print(f"   ID: {response['id']}")
    print(f"   Titular: {response['cardholder_name']}")
    print(f"   Tarjeta: {response['card_brand']} ****{response['card_last4']}")
    print(f"   Expiración: {response['expiration']}")
    print(f"   Creada: {response['created_at']}")
    print(f"   🔓 Datos desencriptados y devueltos en texto plano")
    print("="*60 + "\n")
    logger.warning(f"[company-card] GET exitoso (last4={model.card_last4}, brand={model.card_brand})")
    return response


@router.delete(
    "",
    status_code=204,
    summary="Eliminar la tarjeta de la empresa",
)
async def delete_company_card(
    db: AsyncSession = Depends(get_db),
):
    print("\n" + "="*60)
    print("🗑️  [COMPANY-CARD] DELETE — Eliminar tarjeta de empresa")
    print("="*60)

    result = await db.execute(
        select(TarjetaEmpresaModel).where(TarjetaEmpresaModel.is_active == True)
    )
    model = result.scalar_one_or_none()

    if not model:
        print("❌ No hay tarjeta registrada → 404")
        print("="*60 + "\n")
        raise HTTPException(status_code=404, detail="No hay tarjeta de empresa registrada")

    print(f"🗑️  Eliminando tarjeta id={model.id}, last4={model.card_last4}, brand={model.card_brand}")
    await db.execute(
        delete(TarjetaEmpresaModel).where(TarjetaEmpresaModel.id == model.id)
    )
    await db.commit()
    print(f"✅ Tarjeta ELIMINADA de la DB")
    print("="*60 + "\n")
    logger.warning(f"[company-card] Tarjeta ELIMINADA (id={model.id}, last4={model.card_last4})")



# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------

def _to_response(model: TarjetaEmpresaModel) -> dict:
    """Construye la respuesta desencriptando los datos sensibles."""
    return {
        "id": model.id,
        "card_number": _decrypt(model.card_number_enc),
        "expiration": model.expiration,
        "cvc": _decrypt(model.cvc_enc),
        "cardholder_name": model.cardholder_name,
        "card_last4": model.card_last4,
        "card_brand": model.card_brand,
        "description": model.description or "",
        "is_active": model.is_active,
        "created_at": model.created_at.isoformat() if model.created_at else "",
        "updated_at": model.updated_at.isoformat() if model.updated_at else "",
    }
