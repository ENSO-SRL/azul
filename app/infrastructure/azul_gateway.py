"""
Azul Payment Gateway — HTTP adapter.

Wraps all communication with the Azul JSON API (mTLS + Auth headers).
"""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx

from app.domain.entities import Payment, PaymentStatus, Transaction
from app.infrastructure.azul_config import load_azul_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AZUL_SANDBOX_URL = "https://pruebas.azul.com.do/webservices/JSON/default.aspx"
_TIMEOUT = 30.0

AuthMode = Literal["splitit", "3dsecure"]


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class AzulPaymentGateway:
    """Stateless adapter — builds a fresh httpx client per call."""

    # -- client factory ---------------------------------------------------

    @staticmethod
    def _build_client(auth_mode: AuthMode = "splitit") -> httpx.AsyncClient:
        cfg = load_azul_config()
        auth1, auth2 = cfg.auth_splitit if auth_mode == "splitit" else cfg.auth_3dsecure
        return httpx.AsyncClient(
            cert=(cfg.cert_path, cfg.key_path),
            timeout=_TIMEOUT,
            headers={
                "Content-Type": "application/json",
                "Auth1": auth1,
                "Auth2": auth2,
            },
        )

    # -- payload builders -------------------------------------------------

    @staticmethod
    def _base_payload(payment: Payment) -> dict[str, Any]:
        cfg = load_azul_config()
        return {
            "Channel": "EC",
            "Store": cfg.merchant_id,
            "PosInputMode": "E-Commerce",
            "TrxType": "Sale",
            "Amount": str(int(payment.amount)),
            "Itbis": str(int(payment.itbis)).zfill(3) if payment.itbis else "000",
            "CurrencyPosCode": payment.currency,
            "Payments": "1",
            "Plan": "0",
            "AcquirerRefData": "1",
            "OrderNumber": payment.order_id or "",
            "CustomerServicePhone": "",
            "ECommerceUrl": "https://atlas.do",
            "CustomOrderId": payment.id,
        }

    # -- public methods ---------------------------------------------------

    async def sale(
        self,
        payment: Payment,
        card_number: str,
        expiration: str,
        cvc: str,
        save_token: bool = False,
    ) -> tuple[Payment, Transaction]:
        """Execute a Sale against Azul and return updated payment + audit txn."""

        payload = self._base_payload(payment)
        payload.update({
            "CardNumber": card_number,
            "Expiration": expiration,
            "CVC": cvc,
            "SaveToDataVault": "1" if save_token else "0",
            "DataVaultToken": "",
            "ForceNo3DS": "1" if payment.auth_mode == "splitit" else "0",
        })

        return await self._execute(payment, payload)

    async def sale_with_token(
        self,
        payment: Payment,
        token: str,
    ) -> tuple[Payment, Transaction]:
        """Charge a previously tokenised card via DataVault token."""

        payload = self._base_payload(payment)
        payload.update({
            "CardNumber": "",
            "Expiration": "",
            "CVC": "",
            "SaveToDataVault": "0",
            "DataVaultToken": token,
            "ForceNo3DS": "1",
        })

        return await self._execute(payment, payload)

    async def smoke_test(self) -> dict[str, Any]:
        """Quick $1.00 test sale — used by /test/smoke endpoint."""

        cfg = load_azul_config()
        payload = {
            "Channel": "EC",
            "Store": cfg.merchant_id,
            "CardNumber": "4260550061845872",
            "Expiration": "203412",
            "CVC": "123",
            "PosInputMode": "E-Commerce",
            "TrxType": "Sale",
            "Amount": "100",
            "Itbis": "000",
            "CurrencyPosCode": "$",
            "Payments": "1",
            "Plan": "0",
            "AcquirerRefData": "1",
            "OrderNumber": "smoke-001",
            "CustomerServicePhone": "",
            "ECommerceUrl": "https://atlas.do",
            "CustomOrderId": "smoke-test",
            "DataVaultToken": "",
            "SaveToDataVault": "0",
            "ForceNo3DS": "1",
        }

        async with self._build_client("splitit") as client:
            resp = await client.post(AZUL_SANDBOX_URL, json=payload)
            resp.raise_for_status()
            return resp.json()

    # -- internal ---------------------------------------------------------

    async def _execute(
        self, payment: Payment, payload: dict[str, Any]
    ) -> tuple[Payment, Transaction]:
        """Send payload to Azul, parse response, return updated entities."""

        request_json = json.dumps(payload, ensure_ascii=False)

        async with self._build_client(payment.auth_mode) as client:
            resp = await client.post(AZUL_SANDBOX_URL, json=payload)

        response_json = resp.text
        data: dict[str, Any] = resp.json() if resp.status_code == 200 else {}

        iso_code = data.get("IsoCode", "")
        message = data.get("ResponseMessage", data.get("ErrorDescription", ""))

        # Update payment entity
        payment.iso_code = iso_code
        payment.response_message = message
        payment.azul_order_id = data.get("AzulOrderId", "")
        payment.card_number_masked = data.get("CardNumber", "")[:4] if data.get("CardNumber") else ""

        if iso_code == "00":
            payment.status = PaymentStatus.APPROVED
        elif iso_code:
            payment.status = PaymentStatus.DECLINED
        else:
            payment.status = PaymentStatus.ERROR

        # Build audit transaction
        txn = Transaction(
            payment_id=payment.id,
            request_payload=request_json,
            response_payload=response_json,
            http_status=resp.status_code,
            iso_code=iso_code,
            response_message=message,
        )

        return payment, txn
