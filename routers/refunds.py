"""
Refund / void endpoints — payment cancellation.

POST /api/v1/payments/{payment_id}/cancel

Auto-selects between Void (≤20 min) and Refund (>20 min) based on the
time elapsed since the original payment approval, as required by Azul doc
(page 19):

  "Las ventas realizadas con la transacción 'Sale' son capturadas
   automáticamente... sólo pueden ser anuladas con una transacción de
   'Void' en un lapso de no más de 20 minutos luego de recibir respuesta
   de aprobación."
"""

from __future__ import annotations

from datetime import datetime, timezone

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import Payment, PaymentStatus, PaymentType
from app.infrastructure.azul_gateway import AzulIntegrationError, AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import (
    SQLPaymentRepository,
    SQLTransactionRepository,
)

router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])

# Window within which Void can be used (Azul requirement: 20 minutes)
_VOID_WINDOW_SECONDS = 20 * 60


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CancelResponse(BaseModel):
    payment_id: str
    action: str          # "void" or "refund"
    status: str
    iso_code: str
    response_message: str
    azul_order_id: str


class RefundRequest(BaseModel):
    amount: int | None = Field(
        None, gt=0,
        description=(
            "Monto a devolver en centavos. "
            "Si se omite, se realiza devolución completa del monto original."
        ),
    )


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def _get_service(db: AsyncSession = Depends(get_db)):
    return (
        SQLPaymentRepository(db),
        SQLTransactionRepository(db),
        AzulPaymentGateway(),
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post(
    "/{payment_id}/cancel",
    response_model=CancelResponse,
    summary="Cancelar / devolver pago (Void ≤20 min | Refund >20 min)",
    description=(
        "Selecciona automáticamente entre **Void** y **Refund** según el tiempo "
        "transcurrido desde la aprobación del pago original.\n\n"
        "- **Void**: dentro de los primeros 20 minutos → anula sin cargo.\n"
        "- **Refund**: después de 20 minutos → devolución."
    ),
)
async def cancel_payment(
    payment_id: str,
    body: RefundRequest = RefundRequest(),
    deps=Depends(_get_service),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    payment_repo, txn_repo, gateway = deps
    from app.services.refund_service import refund_payment
    from app.services.billing_attempts import BillingConflict
    try:
        return await refund_payment(getattr(payment_repo,'_session',None), payment_id, body.amount, idempotency_key, gateway)
    except BillingConflict as exc:
        raise HTTPException(409,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502,'No se confirmó la devolución; consulta su estado antes de repetirla.') from exc
