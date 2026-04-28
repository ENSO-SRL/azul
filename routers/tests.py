"""
Test endpoints para ejecutar flujos desde Swagger.
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_db
from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.repo_impl import SQLPaymentRepository, SQLTransactionRepository
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/api/v1/tests", tags=["Tests (Swagger)"])


def _azul_order_number() -> str:
    """Genera un OrderNumber numérico corto para evitar VALIDATION_ERROR."""
    return str(uuid.uuid4().int % 1_000_000).zfill(6)


class HoldPostVerifyRequest(BaseModel):
    amount: int = Field(1075, description="Monto en centavos")
    itbis: int = Field(121, description="ITBIS en centavos")
    card_number: str = Field("4260550061845872")
    expiration: str = Field("203412", description="YYYYMM")
    cvc: str = Field("123")
    cardholder_name: str = Field("Swagger Test")
    cardholder_email: str = Field("swagger.test@atlas.do")


class HoldPostVerifyResponse(BaseModel):
    hold_payment_id: str
    hold_azul_order_id: str
    hold_iso_code: str
    post_payment_id: str
    post_azul_order_id: str
    post_iso_code: str
    verify_hold_found: bool
    verify_post_found: bool


def _get_service(db: AsyncSession = Depends(get_db)) -> PaymentService:
    return PaymentService(
        payment_repo=SQLPaymentRepository(db),
        txn_repo=SQLTransactionRepository(db),
        gateway=AzulPaymentGateway(),
        card_repo=SQLSavedCardRepository(db),
    )


@router.post(
    "/hold-post-verify",
    response_model=HoldPostVerifyResponse,
    summary="Ejecutar Hold + Post + VerifyPayment en un solo paso",
    description=(
        "Endpoint de prueba para Swagger. Ejecuta un Hold, luego su Post, y finalmente "
        "verifica ambas transacciones por CustomOrderId con VerifyPayment."
    ),
)
async def test_hold_post_verify(
    body: HoldPostVerifyRequest,
    svc: PaymentService = Depends(_get_service),
):
    if os.getenv("AZUL_ENV", "sandbox") == "production":
        raise HTTPException(status_code=403, detail="Tests deshabilitados en producción")

    hold_order = _azul_order_number()
    post_order = _azul_order_number()

    hold = await svc.process_hold(
        amount=body.amount,
        itbis=body.itbis,
        card_number=body.card_number,
        expiration=body.expiration,
        cvc=body.cvc,
        order_id=hold_order,
        cardholder_name=body.cardholder_name,
        cardholder_email=body.cardholder_email,
        idempotency_key=f"swg-hold-{uuid.uuid4().hex}",
    )
    if not hold.azul_order_id:
        raise HTTPException(status_code=502, detail="Hold sin AzulOrderId; no se puede hacer Post")

    post = await svc.process_post(
        amount=body.amount,
        itbis=body.itbis,
        azul_order_id=hold.azul_order_id,
        cvc=body.cvc,
        order_id=post_order,
        cardholder_name=body.cardholder_name,
        cardholder_email=body.cardholder_email,
        idempotency_key=f"swg-post-{uuid.uuid4().hex}",
    )

    verify_hold = await svc.verify_payment(custom_order_id=hold_order)
    verify_post = await svc.verify_payment(custom_order_id=post_order)

    return {
        "hold_payment_id": hold.id,
        "hold_azul_order_id": hold.azul_order_id,
        "hold_iso_code": hold.iso_code,
        "post_payment_id": post.id,
        "post_azul_order_id": post.azul_order_id,
        "post_iso_code": post.iso_code,
        "verify_hold_found": verify_hold.get("Found") in (True, "true", "True"),
        "verify_post_found": verify_post.get("Found") in (True, "true", "True"),
    }
