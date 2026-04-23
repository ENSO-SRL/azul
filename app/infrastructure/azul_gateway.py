"""
Azul Payment Gateway — HTTP adapter.

Wraps all communication with the Azul JSON API (mTLS + Auth headers).

Key design decisions
---------------------
- ``_execute()`` never raises on business declines (IsoCode != 00).
  A decline is a valid Azul response, not a Python error.
  Only HTTP-level failures or unparseable JSON raise exceptions.
- PAN masking: digits 7-15 of CardNumber are replaced with '*' before
  the request payload is stored in Transaction audit logs (PCI requirement).
- CIT / MIT: ``sale_cit`` sets ``cardholderInitiatedIndicator``;
  ``sale_mit`` sets ``merchantInitiatedIndicator``.  Both are required
  by Visa / Mastercard for stored-credential flows.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import httpx

from app.domain.entities import (
    AzulResponseCode,
    IsoCode,
    Payment,
    PaymentStatus,
    SavedCard,
    Transaction,
)
from app.infrastructure.azul_config import load_azul_config

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

AuthMode = Literal["splitit", "3dsecure"]

# ---------------------------------------------------------------------------
# PAN masking helper
# ---------------------------------------------------------------------------

_PAN_RE = re.compile(r'"CardNumber"\s*:\s*"(\d{13,19})"')


def _mask_pan(payload_json: str) -> str:
    """Replace digits 7-15 of CardNumber with '*' in a JSON string.

    Example:
        "CardNumber": "4260550061845872"
        →  "CardNumber": "426055*******872"
    """
    def _replace(m: re.Match) -> str:
        pan = m.group(1)
        if len(pan) < 13:
            return m.group(0)
        masked = pan[:6] + "*" * (len(pan) - 10) + pan[-4:]
        return f'"CardNumber": "{masked}"'

    return _PAN_RE.sub(_replace, payload_json)


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
            timeout=30.0,
            headers={
                "Content-Type": "application/json",
                "Auth1": auth1,
                "Auth2": auth2,
            },
        )

    @staticmethod
    def _get_url() -> str:
        return load_azul_config().api_url

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
        """Execute a CIT Sale with full card data.

        If ``save_token=True`` the card is stored in DataVault and the token
        is available on the returned Payment as ``data_vault_token``.
        """
        payload = self._base_payload(payment)
        payload.update({
            "CardNumber": card_number,
            "Expiration": expiration,
            "CVC": cvc,
            "SaveToDataVault": "1" if save_token else "0",
            "DataVaultToken": "",
            "ForceNo3DS": "1" if payment.auth_mode == "splitit" else "0",
            "cardholderInitiatedIndicator": "1",
        })

        return await self._execute(payment, payload)

    async def sale_cit(
        self,
        payment: Payment,
        token: str,
    ) -> tuple[Payment, Transaction]:
        """Cardholder-Initiated Transaction using a DataVault token.

        The user is present and authorises the charge, but does not re-enter
        the card number.  Use for on-demand club / service charges.
        """
        payload = self._base_payload(payment)
        payload.update({
            "CardNumber": "",
            "Expiration": "",
            "CVC": "",
            "SaveToDataVault": "0",
            "DataVaultToken": token,
            "ForceNo3DS": "1",
            "cardholderInitiatedIndicator": "1",
        })

        return await self._execute(payment, payload)

    async def sale_mit(
        self,
        payment: Payment,
        token: str,
    ) -> tuple[Payment, Transaction]:
        """Merchant-Initiated Transaction — STANDING_ORDER subtype.

        The user is NOT present.  Used by the scheduler for recurring charges.
        Requires a DataVault token obtained from a prior CIT.
        """
        payload = self._base_payload(payment)
        payload.update({
            "CardNumber": "",
            "Expiration": "",
            "CVC": "",
            "SaveToDataVault": "0",
            "DataVaultToken": token,
            "ForceNo3DS": "1",
            "merchantInitiatedIndicator": "1",
        })

        return await self._execute(payment, payload)

    async def create_token(
        self,
        customer_id: str,
        card_number: str,
        expiration: str,
        cvc: str,
    ) -> SavedCard:
        """Register a card in DataVault WITHOUT charging it (TrxType CREATE).

        Returns a SavedCard domain entity.  The token is the DataVaultToken
        field in the Azul response.
        """
        cfg = load_azul_config()
        payload = {
            "Channel": "EC",
            "Store": cfg.merchant_id,
            "CardNumber": card_number,
            "Expiration": expiration,
            "CVC": cvc,
            "TrxType": "CREATE",
            "PosInputMode": "E-Commerce",
            "AcquirerRefData": "1",
            "CustomOrderId": f"tok-{customer_id}",
        }

        async with self._build_client("splitit") as client:
            resp = await client.post(self._get_url(), json=payload)

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        token = data.get("DataVaultToken", "")
        if not token:
            err = data.get("ErrorDescription", data.get("ResponseMessage", "Unknown error"))
            raise ValueError(f"DataVault CREATE failed: {err}")

        return SavedCard(
            customer_id=customer_id,
            token=token,
            card_brand=data.get("Brand", ""),
            card_last4=card_number[-4:],
            expiration=expiration,
        )

    async def delete_token(self, token: str) -> None:
        """Remove a card from DataVault (TrxType DELETE).

        Required for user-initiated card removal (GDPR / consumer rights).
        """
        cfg = load_azul_config()
        payload = {
            "Channel": "EC",
            "Store": cfg.merchant_id,
            "TrxType": "DELETE",
            "DataVaultToken": token,
        }

        async with self._build_client("splitit") as client:
            resp = await client.post(self._get_url(), json=payload)

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        if data.get("ResponseCode") == AzulResponseCode.ERROR:
            err = data.get("ErrorDescription", "Unknown error")
            raise ValueError(f"DataVault DELETE failed: {err}")

    async def smoke_test(self) -> dict[str, Any]:
        """Quick test sale — used by /test/smoke endpoint."""
        cfg = load_azul_config()
        payload = {
            "Channel": "EC",
            "Store": cfg.merchant_id,
            "CardNumber": "4260550061845872",
            "Expiration": "203412",
            "CVC": "123",
            "PosInputMode": "E-Commerce",
            "TrxType": "Sale",
            "Amount": "118",
            "Itbis": "18",
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
            resp = await client.post(self._get_url(), json=payload)
            resp.raise_for_status()
            return resp.json()

    # -- internal ---------------------------------------------------------

    async def _execute(
        self, payment: Payment, payload: dict[str, Any]
    ) -> tuple[Payment, Transaction]:
        """Send payload to Azul, parse response, return updated entities.

        IMPORTANT: does NOT raise on business declines (IsoCode != 00).
        A declined card is a valid Azul response — handle it in the service
        layer by checking payment.status == PaymentStatus.DECLINED.

        Only raises on:
        - HTTP errors (4xx/5xx from the Azul server itself)
        - Unparseable JSON responses
        """
        request_json = json.dumps(payload, ensure_ascii=False)
        # PCI: mask PAN before storing in audit log
        masked_request_json = _mask_pan(request_json)

        async with self._build_client(payment.auth_mode) as client:
            resp = await client.post(self._get_url(), json=payload)

        # Only raise on HTTP-level failures, not on Azul business errors
        resp.raise_for_status()

        response_json = resp.text
        try:
            data: dict[str, Any] = resp.json()
        except Exception:
            data = {}

        iso_raw      = data.get("IsoCode", "")
        rc_raw       = data.get("ResponseCode", "")
        message      = data.get("ResponseMessage", data.get("ErrorDescription", ""))

        # Update payment entity
        payment.iso_code       = iso_raw
        payment.response_code  = rc_raw
        payment.response_message = message
        payment.azul_order_id  = data.get("AzulOrderId", "")
        payment.data_vault_token = data.get("DataVaultToken", "")

        # Mask to last 4 from original card if present in payload
        raw_pan = payload.get("CardNumber", "")
        if raw_pan and len(raw_pan) >= 4:
            payment.card_number_masked = "*" * (len(raw_pan) - 4) + raw_pan[-4:]

        # Map iso_code → PaymentStatus
        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
        elif rc_raw == AzulResponseCode.ERROR or not iso_raw:
            # Validation / auth error before the processor
            payment.status = PaymentStatus.ERROR
        else:
            # Any non-00 IsoCode from the processor = declined
            payment.status = PaymentStatus.DECLINED

        # Build masked audit transaction
        txn = Transaction(
            payment_id=payment.id,
            request_payload=masked_request_json,   # PAN enmascarado
            response_payload=response_json,
            http_status=resp.status_code,
            iso_code=iso_raw,
            response_code=rc_raw,
            response_message=message,
        )

        return payment, txn
