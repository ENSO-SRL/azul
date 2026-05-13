# -*- coding: utf-8 -*-
"""
Script de diagnóstico 3DS — muestra la respuesta RAW del Sale y ProcessThreeDSMethod.
Ejecutar: py scripts/debug_3ds.py
"""
from __future__ import annotations
import asyncio, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("AZUL_LOCAL_MODE", "1")

import httpx
from app.infrastructure.azul_config import load_azul_config
from app.domain.entities import Currency, Payment, PaymentType
from app.infrastructure.azul_gateway import AzulPaymentGateway, _post_with_failover

EXPIRATION = "203412"
CVC        = "123"
CARD_3DS   = "4265880000000007"   # Frictionless + Method

BROWSER_INFO = {
    "accept_header": "text/html,application/xhtml+xml",
    "ip_address": "127.0.0.1",
    "language": "es-DO",
    "color_depth": "24",
    "screen_width": "1920",
    "screen_height": "1080",
    "time_zone": "240",
    "user_agent": "Atlas-Certification/1.0",
    "javascript_enabled": "true",
}


async def main():
    cfg = load_azul_config()
    gw  = AzulPaymentGateway()

    p = Payment(
        amount=10000, itbis=1500,
        payment_type=PaymentType.SALE,
        auth_mode="3dsecure",
        cardholder_name="Debug Test",
        cardholder_email="debug@iamatlas.do",
        currency_code=Currency.DOP,
    )

    print("\n── Paso 2: Sale 3DS ─────────────────────────────────────────")
    p, _ = await gw.sale(p, CARD_3DS, EXPIRATION, CVC,
                          browser_info=BROWSER_INFO, include_method_notification_url=True)
    print(f"  iso_code      : {p.iso_code}")
    print(f"  azul_order_id : {p.azul_order_id!r}")
    print(f"  status        : {p.status}")

    azul_oid = p.azul_order_id
    if not azul_oid:
        print("  ERROR: azul_order_id está vacío — Azul no lo retornó en la respuesta.")
        return

    print(f"\n── Paso 5: ProcessThreeDSMethod con AZULOrderId={azul_oid!r} ──")
    payload = {
        "Channel": "EC",
        "Store": cfg.merchant_id,
        "AZULOrderId": azul_oid,
        "MethodNotificationStatus": "EXPECTED_BUT_NOT_RECEIVED",
    }
    print(f"  Payload enviado: {json.dumps(payload, indent=2)}")

    async with gw._build_client("3dsecure") as client:
        resp = await client.post(cfg.threeds_method_url, json=payload)

    print(f"\n  HTTP Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"  Response RAW:\n{json.dumps(data, indent=2, ensure_ascii=False)}")
    except Exception:
        print(f"  Response text: {resp.text[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
