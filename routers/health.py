"""
Health check + smoke test endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.infrastructure.azul_gateway import AzulPaymentGateway

router = APIRouter(tags=["Health"])


@router.get("/health", summary="Health check")
async def health():
    return {"status": "ok"}


@router.post("/test/smoke", summary="Smoke test against Azul sandbox")
async def smoke_test():
    """Fire a $1.00 test Sale to the Azul sandbox.

    Uses test card 4260550061845872 with splitit auth mode.
    IsoCode '00' = success.
    """
    gw = AzulPaymentGateway()
    result = await gw.smoke_test()
    return {
        "iso_code": result.get("IsoCode", ""),
        "response_message": result.get("ResponseMessage", ""),
        "azul_order_id": result.get("AzulOrderId", ""),
        "approved": result.get("IsoCode") == "00",
        "raw": result,
    }
