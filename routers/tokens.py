"""
Token endpoints — DataVault card management.

POST   /api/v1/tokens                 → Register a card (TrxType CREATE, no charge)
GET    /api/v1/tokens/{customer_id}   → List saved cards for a customer
DELETE /api/v1/tokens/{token}         → Remove a card from DataVault + local DB
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
    summary="Eliminar tarjeta de DataVault",
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
