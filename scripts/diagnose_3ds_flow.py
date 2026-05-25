"""
Diagnóstico 3DS: Simula el flujo completo de 3DS 2.0 para identificar
en qué paso exactamente ocurre el error 'DataVault not enabled'.

1. Hace un Sale → obtiene 3D2METHOD + AZULOrderId
2. Llama processthreedsmethod con ese AZULOrderId
3. Muestra la respuesta completa de cada paso

Ejecutar desde la raíz del proyecto:
  python scripts/diagnose_3ds_flow.py
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

logging.basicConfig(level=logging.WARNING, format="%(message)s")

import httpx
from app.infrastructure.azul_config import load_azul_config, AZUL_URL_PRODUCTION


async def _post(client: httpx.AsyncClient, url: str, payload: dict, label: str) -> dict:
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"URL: {url}")
    print(f"Payload keys: {list(payload.keys())}")
    print(f"{'='*60}")

    try:
        resp = await client.post(url, json=payload)
        data = resp.json()
    except Exception as e:
        print(f"HTTP ERROR: {e}")
        return {}

    print(f"HTTP Status : {resp.status_code}")
    print(f"ResponseCode: {data.get('ResponseCode', '?')}")
    print(f"IsoCode     : {data.get('IsoCode', '?')}")
    print(f"ResponseMsg : {data.get('ResponseMessage', '?')}")
    print(f"ErrorDesc   : {data.get('ErrorDescription', '(none)')}")
    print(f"Full:\n{json.dumps(data, indent=2, ensure_ascii=False)[:1200]}")
    return data


async def main():
    cfg = load_azul_config()
    print(f"\n[CONFIG] env={cfg.env}, merchant={cfg.merchant_id}")

    auth1_split, auth2_split = cfg.auth_splitit
    auth1_3ds, auth2_3ds = cfg.auth_3dsecure

    # --- STEP 1: Sale con auth_mode=splitit (como el checkout) ---
    sale_payload = {
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
        "OrderNumber": "DIAG3DS-001",
        "CustomerServicePhone": "",
        "ECommerceUrl": "https://atlas.do",
        "CustomOrderId": "diag-3ds-001",
        "CardHolderName": "TEST USER",
        "CardHolderEmail": "test@atlas.do",
        "CardNumber": "4260550061845872",
        "Expiration": "203412",
        "CVC": "123",
        "cardholderInitiatedIndicator": "1",
        # Sin ForceNo3DS en produccion, sin DataVault
    }

    async with httpx.AsyncClient(
        cert=(cfg.cert_path, cfg.key_path),
        timeout=30.0,
        headers={
            "Content-Type": "application/json",
            "Auth1": auth1_split,
            "Auth2": auth2_split,
        }
    ) as client:
        step1 = await _post(client, AZUL_URL_PRODUCTION, sale_payload, "SALE (splitit auth)")
        azul_order_id = step1.get("AZULOrderId") or step1.get("AzulOrderId", "")
        print(f"\n>>> AZULOrderId para 3DS: {azul_order_id!r}")

    if not azul_order_id:
        print("\n[ABORT] Sin AZULOrderId — no se puede continuar con 3DS.")
        return

    # --- STEP 2: processthreedsmethod con auth_mode=3dsecure ---
    method_url_prod = "https://pagos.azul.com.do/WebServices/JSON/default.aspx?processthreedsmethod"
    method_payload = {
        "Channel": "EC",
        "Store": cfg.merchant_id,
        "AZULOrderId": azul_order_id,
        "AzulOrderId": azul_order_id,
        "MethodNotificationStatus": "EXPECTED_BUT_NOT_RECEIVED",  # simula timeout del iframe
    }

    async with httpx.AsyncClient(
        cert=(cfg.cert_path, cfg.key_path),
        timeout=30.0,
        headers={
            "Content-Type": "application/json",
            "Auth1": auth1_3ds,
            "Auth2": auth2_3ds,
        }
    ) as client:
        step2 = await _post(client, method_url_prod, method_payload, "PROCESS 3DS METHOD (3dsecure auth)")

    print("\n\n[DIAGNOSIS]")
    if step2.get("ResponseCode") == "Error":
        print(f"FALLA en processthreedsmethod: {step2.get('ErrorDescription')}")
        print("Causa probable: las credenciales 3DS (AZUL_AUTH1_3DS) son incorrectas")
        print("              o el merchant no tiene 3DS habilitado correctamente.")
    elif step2.get("IsoCode") == "00":
        print("OK: 3DS completado - pago APROBADO sin challenge")
    elif step2.get("IsoCode") == "3D2CHAL":
        print("OK: 3DS requiere challenge con el banco")
    else:
        print(f"Resultado: IsoCode={step2.get('IsoCode')} ResponseCode={step2.get('ResponseCode')}")


if __name__ == "__main__":
    asyncio.run(main())
