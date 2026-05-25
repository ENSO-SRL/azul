"""
Script de diagnóstico rápido: carga el código NUEVO directamente (sin servidor HTTP)
y muestra exactamente qué payload se enviaría a Azul para detectar DataVault fields.
"""
import asyncio
import json
import os
import sys
import logging

# Setup logging BEFORE imports
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.infrastructure.azul_config import _load_dotenv, load_azul_config
_load_dotenv()

from app.domain.entities import Payment, PaymentType
from app.infrastructure.azul_gateway import AzulPaymentGateway, _datavault_fields, _force_no3ds, _mask_sensitive

def build_test_payload():
    cfg = load_azul_config()
    print(f"\n=== CONFIG ===")
    print(f"  env       = {cfg.env}")
    print(f"  merchant  = {cfg.merchant_id}")
    print(f"  cert_path = {cfg.cert_path}")
    print(f"  key_path  = {cfg.key_path}")
    print(f"  app_base  = {cfg.app_base_url}")
    print()

    payment = Payment(
        amount=200,
        itbis=36,
        payment_type=PaymentType.SALE,
        order_id="CHK-TEST01",
        auth_mode="3dsecure",
        initiated_by="cardholder",
        cardholder_name="TEST USER",
        cardholder_email="test@atlas.do",
    )

    gw = AzulPaymentGateway()
    base = gw._base_payload(payment)

    force = _force_no3ds(cfg, "0")   # auth_mode=3dsecure → "0"
    dv    = _datavault_fields(False)  # save_card=False

    print(f"=== _force_no3ds(cfg, '0') ===  {force}")
    print(f"=== _datavault_fields(False) === {dv}")

    payload = {
        **base,
        "CardNumber": "4260550061845872",
        "Expiration": "202812",
        "CVC": "123",
        **dv,
        **force,
        "cardholderInitiatedIndicator": "1",
    }

    browser_info = {
        "accept_header": "text/html",
        "ip_address": "127.0.0.1",
        "language": "es-DO",
        "color_depth": "24",
        "screen_width": "1280",
        "screen_height": "720",
        "time_zone": "240",
        "user_agent": "Mozilla/5.0 TestAgent",
        "javascript_enabled": "true",
    }

    if payment.auth_mode == "3dsecure" and browser_info:
        term_url    = f"{cfg.app_base_url}/api/v1/3ds/term?payment_id={payment.id}"
        method_url  = f"{cfg.app_base_url}/api/v1/3ds/method-notification?payment_id={payment.id}"
        payload["ThreeDSAuth"] = {
            "TermUrl": term_url,
            "RequestorChallengeIndicator": "01",
            "MethodNotificationUrl": method_url,
        }
        payload["BrowserInfo"] = {
            "AcceptHeader": browser_info["accept_header"],
            "IPAddress": browser_info["ip_address"],
            "Language": browser_info["language"],
            "ColorDepth": browser_info["color_depth"],
            "ScreenWidth": browser_info["screen_width"],
            "ScreenHeight": browser_info["screen_height"],
            "TimeZone": browser_info["time_zone"],
            "UserAgent": browser_info["user_agent"],
            "JavaScriptEnabled": browser_info["javascript_enabled"],
        }

    print("\n=== PAYLOAD KEYS ===")
    for k, v in payload.items():
        if k in ("CardNumber", "CVC"):
            print(f"  {k}: {_mask_sensitive(json.dumps({k: v}))}")
        elif k in ("ThreeDSAuth", "BrowserInfo"):
            print(f"  {k}: {json.dumps(v, indent=4)}")
        else:
            print(f"  {k}: {v!r}")

    # Check for DataVault triggers
    print("\n=== DATAVAULT CHECK ===")
    if "SaveToDataVault" in payload:
        print(f"  ⚠ SaveToDataVault = {payload['SaveToDataVault']!r}  ← WILL TRIGGER DataVault error")
    else:
        print(f"  ✓ SaveToDataVault NOT in payload")
    if "DataVaultToken" in payload:
        print(f"  ⚠ DataVaultToken = {payload['DataVaultToken']!r}  ← WILL TRIGGER DataVault error")
    else:
        print(f"  ✓ DataVaultToken NOT in payload")

    return payload


if __name__ == "__main__":
    build_test_payload()
