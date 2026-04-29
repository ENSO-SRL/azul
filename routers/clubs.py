"""
Club payment endpoint — on-demand CIT charges using a stored token.

POST /api/v1/clubs/{club_id}/pay

The user is present (tapped "Pagar" in the app) but does not re-enter
their card — the charge uses their saved DataVault token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import (
    SQLPaymentRepository,
    SQLTransactionRepository,
)
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/api/v1/clubs", tags=["Clubs"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ClubPayRequest(BaseModel):
    customer_id: str = Field(..., description="ID del cliente en Atlas")
    token: str        = Field(..., description="DataVault token de la tarjeta guardada")
    amount: int       = Field(..., description="Monto en centavos (ej. 5000 = $50.00)")
    itbis: int        = Field(0,   description="ITBIS en centavos")
    currency: str     = Field("DOP", description="Moneda: DOP (peso) o USD")

    model_config = {"json_schema_extra": {"examples": [
        {
            "customer_id": "usr_12345",
            "token": "129BCAAB-742A-4F64-AF54-8A9F1BAD802C",
            "amount": 5000,
            "itbis": 900,
        }
    ]}}


class ClubPayResponse(BaseModel):
    id: str
    club_id: str
    customer_id: str
    amount: int
    itbis: int
    status: str
    iso_code: str
    response_message: str
    azul_order_id: str
    created_at: str


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
# Route
# ---------------------------------------------------------------------------

@router.post(
    "/{club_id}/pay",
    response_model=ClubPayResponse,
    summary="Cobrar club on-demand (CIT con token)",
)
async def pay_club(
    club_id: str,
    body: ClubPayRequest,
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    svc: PaymentService  = Depends(_get_service),
):
    """Charge a club membership fee using the customer's stored DataVault token.

    This is a **Cardholder-Initiated Transaction (CIT)** — the user is present
    and authorised the charge but didn't re-enter their card details.

    Pass an ``Idempotency-Key`` header to safely retry without double-charging.
    """
    try:
        payment = await svc.charge_club(
            customer_id=body.customer_id,
            club_id=club_id,
            amount=body.amount,
            itbis=body.itbis,
            token=body.token,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "id": payment.id,
        "club_id": club_id,
        "customer_id": body.customer_id,
        "amount": payment.amount,
        "itbis": payment.itbis,
        "status": payment.status.value,
        "iso_code": payment.iso_code,
        "response_message": payment.response_message,
        "azul_order_id": payment.azul_order_id,
        "created_at": payment.created_at.isoformat(),
    }
