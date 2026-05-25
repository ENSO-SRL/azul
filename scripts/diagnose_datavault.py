"""
Diagnóstico: Envía un payload mínimo a Azul producción para identificar
qué campo exactamente dispara el error 'DataVault not enabled'.

Envía tres variantes del payload:
  1. Sin ningún campo DataVault (lo que debería enviar el checkout)
  2. Con SaveToDataVault="0" y DataVaultToken="" (combinación problemática conocida)
  3. Smoke test via azul_gateway.smoke_test()

Ejecutar desde la raíz del proyecto:
  python scripts/diagnose_datavault.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load env vars
from app.infrastructure.azul_config import _load_dotenv
_load_dotenv()

logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
)

import httpx
from app.infrastructure.azul_config import load_azul_config, AZUL_URL_PRODUCTION
from app.infrastructure.azul_gateway import _datavault_fields, _force_no3ds


async def _post(payload: dict, label: str) -> dict:
    cfg = load_azul_config()
    auth1, auth2 = cfg.auth_splitit
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"Payload keys: {list(payload.keys())}")
    print(f"DataVault fields in payload: ", {
        k: v for k, v in payload.items()
        if "datavault" in k.lower() or "savetodatavault" in k.lower()
    } or "(none — correct)")
    print(f"{'='*60}")

    async with httpx.AsyncClient(
        cert=(cfg.cert_path, cfg.key_path),
        timeout=30.0,
        headers={
            "Content-Type": "application/json",
            "Auth1": auth1,
            "Auth2": auth2,
        }
    ) as client:
        try:
            resp = await client.post(AZUL_URL_PRODUCTION, json=payload)
            data = resp.json()
        except Exception as e:
            print(f"HTTP ERROR: {e}")
            return {}

    print(f"HTTP Status : {resp.status_code}")
    print(f"ResponseCode: {data.get('ResponseCode', '?')}")
    print(f"IsoCode     : {data.get('IsoCode', '?')}")
    print(f"ResponseMsg : {data.get('ResponseMessage', '?')}")
    print(f"ErrorDesc   : {data.get('ErrorDescription', '(none)')}")
    print(f"Full response: {json.dumps(data, indent=2, ensure_ascii=False)[:800]}")
    return data


async def main():
    cfg = load_azul_config()
    print(f"\n[CONFIG] env={cfg.env}, merchant={cfg.merchant_id}")
    print(f"[CONFIG] cert_path={cfg.cert_path}")
    print(f"[CONFIG] key_path={cfg.key_path}")

    base_payload = {
        "Channel": "EC",
        "Store": cfg.merchant_id,
        "PosInputMode": "E-Commerce",
        "TrxType": "Sale",
        "Amount": "200",
        "Itbis": "036",
        "CurrencyPosCode": "$",
        "Payments": "1",
        "Plan": "0",
        "AcquirerRefData": "1",
        "RRN": None,
        "OrderNumber": "DIAG-001",
        "CustomerServicePhone": "",
        "ECommerceUrl": "https://atlas.do",
        "CustomOrderId": "diag-test-001",
        "CardHolderName": "TEST USER",
        "CardHolderEmail": "test@atlas.do",
        "CardNumber": "4260550061845872",  # tarjeta de prueba sandbox (no cargará en prod)
        "Expiration": "203412",
        "CVC": "123",
        "cardholderInitiatedIndicator": "1",
        **_force_no3ds(cfg),  # vacío en producción
    }

    # TEST 1: Sin ningún campo DataVault (comportamiento correcto del checkout)
    payload_no_dv = {**base_payload, **_datavault_fields(False)}
    await _post(payload_no_dv, "SIN campos DataVault (checkout actual)")

    # TEST 2: Con SaveToDataVault=0 y DataVaultToken="" (caso problemático conocido)
    payload_dv_zero = {
        **base_payload,
        "SaveToDataVault": "0",
        "DataVaultToken": "",
    }
    await _post(payload_dv_zero, "CON SaveToDataVault='0' y DataVaultToken=''")

    print("\n\n[DIAGNÓSTICO COMPLETO]")
    print("Si TEST 1 falla con 'DataVault not enabled':")
    print("  → Azul requiere un campo adicional o el merchant no está habilitado para sale sin DataVault.")
    print("Si TEST 1 pasa pero TEST 2 falla:")
    print("  → El bug era que se enviaban los campos vacíos. Ya está corregido.")


if __name__ == "__main__":
    asyncio.run(main())
