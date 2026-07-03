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

from app.infrastructure.azul_gateway import AzulPaymentGateway
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
# NOTA: /by-email/{email} debe ir ANTES de /{customer_id} para que FastAPI
# no interprete "by-email" como un customer_id.
# ---------------------------------------------------------------------------

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
