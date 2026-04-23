"""
Azul Payment Gateway — Async HTTP client.

Provides `build_client()` which returns an httpx.AsyncClient pre-configured
with mTLS certs and the correct Auth1/Auth2 headers for the chosen auth mode.

Auth modes
----------
- ``"splitit"``   → Sale without 3D Secure (sandbox auto-approves).
- ``"3dsecure"``  → Sale with 3D Secure 2.0 challenge flow.

Usage
-----
::

    async with build_client("splitit") as client:
        resp = await client.post(AZUL_URL, json=payload)
"""

from __future__ import annotations

from typing import Literal

import httpx

from azul_config import load_azul_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Azul sandbox endpoint (JSON API)
AZUL_SANDBOX_URL = "https://pruebas.azul.com.do/webservices/JSON/default.aspx"

# Timeout for all Azul calls (seconds)
_TIMEOUT = 30.0

# Type alias for the two valid auth modes
AuthMode = Literal["splitit", "3dsecure"]

# ---------------------------------------------------------------------------
# Client builder
# ---------------------------------------------------------------------------


def build_client(auth_mode: AuthMode = "splitit") -> httpx.AsyncClient:
    """Return an async client configured for the given *auth_mode*.

    Parameters
    ----------
    auth_mode:
        ``"splitit"`` for regular sales or ``"3dsecure"`` for 3DS flow.

    Returns
    -------
    httpx.AsyncClient
        Ready to use as an async context manager.
    """
    cfg = load_azul_config()

    if auth_mode == "splitit":
        auth1, auth2 = cfg.auth_splitit
    elif auth_mode == "3dsecure":
        auth1, auth2 = cfg.auth_3dsecure
    else:
        raise ValueError(f"Unknown auth_mode: {auth_mode!r}. Use 'splitit' or '3dsecure'.")

    return httpx.AsyncClient(
        cert=(cfg.cert_path, cfg.key_path),
        timeout=_TIMEOUT,
        headers={
            "Content-Type": "application/json",
            "Auth1": auth1,
            "Auth2": auth2,
        },
    )


# ---------------------------------------------------------------------------
# Smoke-test helper
# ---------------------------------------------------------------------------


async def test_connection(auth_mode: AuthMode = "splitit") -> dict:
    """Fire a minimal Sale request against the Azul sandbox.

    Uses test card 4260550061845872 (from the Splitit whitelist) with a
    $1.00 charge to validate mTLS + auth headers end-to-end.

    Returns the raw JSON response from Azul.
    """
    cfg = load_azul_config()

    payload = {
        "Channel": "EC",
        "Store": cfg.merchant_id,
        "CardNumber": "4260550061845872",
        "Expiration": "202812",
        "CVC": "123",
        "PosInputMode": "E-Commerce",
        "TrxType": "Sale",
        "Amount": "118",          # $1.18 — base 100 + ITBIS 18 (18%) in cents
        "Itbis": "18",             # 18% ITBIS (Dominican tax) — required by Azul
        "CurrencyPosCode": "$",
        "Payments": "1",
        "Plan": "0",
        "AcquirerRefData": "1",
        "OrderNumber": "test-smoke-001",
        "CustomerServicePhone": "",
        "ECommerceUrl": "https://atlas.do",
        "CustomOrderId": "smoke-test",
        "DataVaultToken": "",
        "SaveToDataVault": "0",
        "ForceNo3DS": "1",        # Skip 3DS for smoke test
    }

    async with build_client(auth_mode) as client:
        resp = await client.post(AZUL_SANDBOX_URL, json=payload)
        resp.raise_for_status()
        return resp.json()
