"""
Azul Payment Gateway — HTTP adapter.

Wraps all communication with the Azul JSON API (mTLS + Auth headers).

Key design decisions
---------------------
- ``_execute()`` never raises on business declines (IsoCode != 00).
  A decline is a valid Azul response, not a Python error.
  Only HTTP-level failures, unparseable JSON, or ResponseCode=Error raise.
- ``AzulIntegrationError`` is raised when Azul returns ResponseCode="Error",
  which indicates a bug in *our* code (bad auth, malformed payload, etc).
  Business declines (IsoCode=51/08/63/99) do NOT raise — check payment.status.
- PAN masking: digits 7-15 of CardNumber are replaced with '*' before
  the request payload is stored in Transaction audit logs (PCI requirement).
- CIT / MIT indicators:
    • ``sale()``    → first charge with full card  → ``cardholderInitiatedIndicator: "1"``
    • ``sale_cit``  → token, user present          → ``cardholderInitiatedIndicator: "STANDING_ORDER"``
    • ``sale_mit``  → token, user NOT present      → ``merchantInitiatedIndicator: "STANDING_ORDER"``
- Timeout: 120 seconds as required by Azul documentation (page 19).
- Failover: production calls attempt primary URL first, then secondary on
  network/timeout errors (required by Azul doc, page 14).
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

AuthMode = Literal["splitit", "3dsecure"]

# ---------------------------------------------------------------------------
# Azul API URLs — failover per environment
# ---------------------------------------------------------------------------

# Production: attempt primary, then secondary on network/timeout failure
AZUL_URLS_PROD = [
    "https://pagos.azul.com.do/webservices/JSON/Default.aspx",
    "https://contpagos.azul.com.do/Webservices/JSON/default.aspx",
]

# Sandbox: single endpoint (no secondary documented)
AZUL_URLS_SANDBOX = [
    "https://pruebas.azul.com.do/webservices/JSON/Default.aspx",
]

# 3DS 2.0 special endpoints (production only)
AZUL_3DS_METHOD_URL    = "https://pagos.azul.com.do/WebServices/JSON/default.aspx?processthreedsmethod"
AZUL_3DS_CHALLENGE_URL = "https://pagos.azul.com.do/WebServices/JSON/default.aspx?processthreedschallenge"

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AzulIntegrationError(Exception):
    """Raised when Azul returns ResponseCode='Error'.

    This means the request itself was malformed or authentication failed —
    it is a bug in *our* integration code, NOT a business decline.

    Examples:
        - MISSING_AUTH_HEADER:Auth1
        - INVALID_AUTH:Auth1
        - VALIDATION_ERROR:Amount
        - INVALID_MERCHANTID

    Callers should:
      - Log + alert (Sentry / SNS / etc.)
      - NOT mark the subscription as paused (it's our fault, not the user's)
      - NOT retry automatically without fixing the root cause
    """


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
# Failover HTTP helper
# ---------------------------------------------------------------------------


async def _post_with_failover(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    env: str,
) -> httpx.Response:
    """POST to Azul with automatic failover to secondary URL on network errors.

    Production: tries primary URL first, then secondary (required by Azul doc p.14).
    Sandbox: single URL, no failover.

    Raises the last exception if all URLs fail.
    """
    urls = AZUL_URLS_PROD if env == "production" else AZUL_URLS_SANDBOX
    last_exc: Exception | None = None

    for url in urls:
        try:
            resp = await client.post(url, json=payload)
            return resp
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
            logger.warning(
                "[azul] failover: %s failed (%s) — trying next URL", url, type(exc).__name__
            )
            continue

    # All URLs exhausted
    raise last_exc  # type: ignore[misc]


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
            timeout=120.0,  # Required by Azul documentation (page 19)
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
            # Required since Azul API v1.2
            "CardHolderName": payment.cardholder_name,
            "CardHolderEmail": payment.cardholder_email,
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
        """Execute a CIT Sale with full card data (first-time charge).

        Uses ``cardholderInitiatedIndicator: "1"`` — cardholder is present
        and entering their card for the first time.

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

        The user is present and authorises the charge but does not re-enter
        their card number.  Use for on-demand club / service charges where
        the user has previously stored their card.

        Uses ``cardholderInitiatedIndicator: "STANDING_ORDER"`` per Azul v1.2.
        """
        payload = self._base_payload(payment)
        payload.update({
            "CardNumber": "",
            "Expiration": "",
            "CVC": "",
            "SaveToDataVault": "0",
            "DataVaultToken": token,
            "ForceNo3DS": "1",
            "cardholderInitiatedIndicator": "STANDING_ORDER",
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

        Uses ``merchantInitiatedIndicator: "STANDING_ORDER"`` per Azul v1.2.
        """
        payload = self._base_payload(payment)
        payload.update({
            "CardNumber": "",
            "Expiration": "",
            "CVC": "",
            "SaveToDataVault": "0",
            "DataVaultToken": token,
            "ForceNo3DS": "1",
            "merchantInitiatedIndicator": "STANDING_ORDER",
        })

        return await self._execute(payment, payload)

    async def create_token(
        self,
        customer_id: str,
        card_number: str,
        expiration: str,
        cvc: str,
        cardholder_name: str = "",
        cardholder_email: str = "",
    ) -> SavedCard:
        """Register a card in DataVault WITHOUT charging it (TrxType CREATE).

        Returns a SavedCard domain entity.  The token is the DataVaultToken
        field in the Azul response.

        Raises:
            AzulIntegrationError: if Azul returns ResponseCode='Error'
            ValueError: if DataVaultToken is missing in the response
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
            "CardHolderName": cardholder_name,
            "CardHolderEmail": cardholder_email,
        }

        async with self._build_client("splitit") as client:
            resp = await _post_with_failover(client, payload, cfg.env)

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        rc = data.get("ResponseCode", "")
        if rc == AzulResponseCode.ERROR:
            err = data.get("ErrorDescription", data.get("ResponseMessage", "Unknown error"))
            raise AzulIntegrationError(f"DataVault CREATE failed: {err}")

        token = data.get("DataVaultToken", "")
        if not token:
            err = data.get("ErrorDescription", data.get("ResponseMessage", "Unknown error"))
            raise ValueError(f"DataVault CREATE: no token in response — {err}")

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

        Raises:
            AzulIntegrationError: if Azul returns ResponseCode='Error'
        """
        cfg = load_azul_config()
        payload = {
            "Channel": "EC",
            "Store": cfg.merchant_id,
            "TrxType": "DELETE",
            "DataVaultToken": token,
        }

        async with self._build_client("splitit") as client:
            resp = await _post_with_failover(client, payload, cfg.env)

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        if data.get("ResponseCode") == AzulResponseCode.ERROR:
            err = data.get("ErrorDescription", "Unknown error")
            raise AzulIntegrationError(f"DataVault DELETE failed: {err}")

    async def void(
        self,
        azul_order_id: str,
        original_date: str,
    ) -> dict[str, Any]:
        """Void (cancel) a same-day transaction within 20 minutes of approval.

        Args:
            azul_order_id: AzulOrderId returned in the original Sale response.
            original_date: Transaction date in YYYYMMDD format.

        Returns:
            Raw Azul response dict.

        Raises:
            AzulIntegrationError: if Azul returns ResponseCode='Error'
        """
        cfg = load_azul_config()
        payload = {
            "Channel": "EC",
            "Store": cfg.merchant_id,
            "AzulOrderId": azul_order_id,
            "OriginalDate": original_date,
            "TrxType": "Void",
        }

        async with self._build_client("splitit") as client:
            resp = await _post_with_failover(client, payload, cfg.env)

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        if data.get("ResponseCode") == AzulResponseCode.ERROR:
            err = data.get("ErrorDescription", "Unknown error")
            raise AzulIntegrationError(f"Void failed: {err}")

        return data

    async def refund(
        self,
        payment: Payment,
        original_date: str,
        azul_order_id: str,
        amount: int | None = None,
    ) -> tuple[Payment, Transaction]:
        """Refund a transaction after the 20-minute void window has passed.

        Args:
            payment: A new Payment entity representing the refund.
            original_date: Transaction date in YYYYMMDD format.
            azul_order_id: AzulOrderId of the original Sale.
            amount: Partial refund amount in centavos. None = full refund.

        Returns:
            Updated payment entity and audit transaction.
        """
        payload = self._base_payload(payment)
        payload.update({
            "TrxType": "Refund",
            "OriginalDate": original_date,
            "AzulOrderId": azul_order_id,
            "CardNumber": "",
            "Expiration": "",
            "CVC": "",
            "DataVaultToken": "",
            "SaveToDataVault": "0",
        })
        if amount is not None:
            payload["Amount"] = str(int(amount))

        return await self._execute(payment, payload)

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
            "CardHolderName": "Test User",
            "CardHolderEmail": "test@atlas.do",
            "cardholderInitiatedIndicator": "1",
        }

        async with self._build_client("splitit") as client:
            resp = await _post_with_failover(client, payload, cfg.env)
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

        Raises:
            AzulIntegrationError: when ResponseCode='Error' — this is OUR bug.
            httpx.HTTPStatusError: on HTTP 4xx/5xx from the Azul server itself.
        """
        cfg = load_azul_config()
        request_json = json.dumps(payload, ensure_ascii=False)
        # PCI: mask PAN before storing in audit log
        masked_request_json = _mask_pan(request_json)

        async with self._build_client(payment.auth_mode) as client:
            resp = await _post_with_failover(client, payload, cfg.env)

        # Only raise on HTTP-level failures, not on Azul business errors
        resp.raise_for_status()

        response_json = resp.text
        try:
            data: dict[str, Any] = resp.json()
        except Exception:
            data = {}

        iso_raw = data.get("IsoCode", "")
        rc_raw  = data.get("ResponseCode", "")
        message = data.get("ResponseMessage", data.get("ErrorDescription", ""))

        # ---------------------------------------------------------------
        # ResponseCode=Error means WE sent a bad request (auth, payload…)
        # This is a bug in our code — raise so callers can alert + fix.
        # ---------------------------------------------------------------
        if rc_raw == AzulResponseCode.ERROR:
            err_desc = data.get("ErrorDescription", message or "Unknown integration error")
            raise AzulIntegrationError(err_desc)

        # Update payment entity
        payment.iso_code         = iso_raw
        payment.response_code    = rc_raw
        payment.response_message = message
        payment.azul_order_id    = data.get("AzulOrderId", "")
        payment.data_vault_token = data.get("DataVaultToken", "")

        # Mask to last 4 from original card if present in payload
        raw_pan = payload.get("CardNumber", "")
        if raw_pan and len(raw_pan) >= 4:
            payment.card_number_masked = "*" * (len(raw_pan) - 4) + raw_pan[-4:]

        # Map iso_code → PaymentStatus
        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
        elif iso_raw in (IsoCode.THREE_DS_CHALLENGE, "3D2METHOD"):
            # 3DS flow — caller must handle the challenge
            payment.status = PaymentStatus.PENDING
        else:
            # Any other non-00 IsoCode from the processor = declined
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
