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

from fastapi import APIRouter, Depends, HTTPException
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
        None,
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
):
    payment_repo, txn_repo, gateway = deps

    # -- Retrieve original payment ----------------------------------------
    original = await payment_repo.get_by_id(payment_id)
    if not original:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} not found")

    if original.status != PaymentStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=f"Payment is {original.status.value} — only APPROVED payments can be cancelled",
        )

    if not original.azul_order_id:
        raise HTTPException(
            status_code=422,
            detail="Payment has no AzulOrderId — cannot cancel without it",
        )

    # -- Determine action: Void or Refund ---------------------------------
    elapsed = (datetime.now(timezone.utc) - original.created_at).total_seconds()
    use_void = elapsed <= _VOID_WINDOW_SECONDS

    original_date = original.created_at.strftime("%Y%m%d")

    try:
        if use_void:
            result = await gateway.void(
                azul_order_id=original.azul_order_id,
                original_date=original_date,
            )
            action = "void"
            iso_code = result.get("IsoCode", "")
            response_message = result.get("ResponseMessage", "")
            azul_order_id = result.get("AzulOrderId", original.azul_order_id)
            # Mark original as cancelled
            original.status = PaymentStatus.DECLINED  # reuse DECLINED for cancelled
            original.response_message = f"[VOID] {response_message}"
            await payment_repo.update(original)

        else:
            # Refund — creates a new payment record
            refund_payment = Payment(
                amount=body.amount if body.amount else original.amount,
                itbis=original.itbis,
                payment_type=PaymentType.SALE,
                order_id=f"refund-{original.order_id}",
                auth_mode=original.auth_mode,
                cardholder_name=original.cardholder_name,
                cardholder_email=original.cardholder_email,
            )
            refund_payment, txn = await gateway.refund(
                payment=refund_payment,
                original_date=original_date,
                azul_order_id=original.azul_order_id,
                amount=body.amount,
            )
            await payment_repo.save(refund_payment)
            await txn_repo.save(txn)
            action = "refund"
            iso_code = refund_payment.iso_code
            response_message = refund_payment.response_message
            azul_order_id = refund_payment.azul_order_id

    except AzulIntegrationError as exc:
        raise HTTPException(status_code=502, detail=f"Integration error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "payment_id": payment_id,
        "action": action,
        "status": "CANCELLED" if use_void else iso_code,
        "iso_code": iso_code,
        "response_message": response_message,
        "azul_order_id": azul_order_id,
    }
