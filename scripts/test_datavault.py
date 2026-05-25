"""
Test DataVault activation — script temporal.

Ejecuta un Sale con SaveToDataVault="1" y verifica si AZUL retorna
un DataVaultToken (activo) o el error "DataVault not enabled" (inactivo).

Uso:
    python scripts/test_datavault.py
"""
import asyncio
import json
import sys
import os

# Asegurar que el root del proyecto esté en el path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.infrastructure.azul_config import load_azul_config
from app.infrastructure.azul_gateway import AzulPaymentGateway, _post_with_failover

import httpx


async def test_datavault():
    cfg = load_azul_config()

    print(f"\n{'='*60}")
    print(f"  TEST DataVault — Merchant: {cfg.merchant_id}")
    print(f"  Ambiente:  {cfg.env.upper()}")
    print(f"  Auth1:     {cfg.auth_splitit[0]}")
    print(f"{'='*60}\n")

    payload = {
        "Channel": "EC",
        "Store": cfg.merchant_id,
        "CardNumber": "4260550061845872",
        "Expiration": "203412",
        "CVC": "123",
        "PosInputMode": "E-Commerce",
        "TrxType": "Sale",
        "Amount": "100",
        "Itbis": "15",
        "CurrencyPosCode": "$",
        "Payments": "1",
        "Plan": "0",
        "AcquirerRefData": "1",
        "SaveToDataVault": "1",
        "DataVaultToken": "",
        "CardHolderName": "Test DataVault",
        "CardHolderEmail": "test@atlas.do",
        "OrderNumber": "test-dv-probe",
        "CustomOrderId": "test-datavault-probe-001",
        "ECommerceUrl": "https://atlas.do",
        "CustomerServicePhone": "",
        "cardholderInitiatedIndicator": "1",
    }

    auth1, auth2 = cfg.auth_splitit

    print(">> Enviando Sale con SaveToDataVault=1 ...")
    print(f"  URL: {'produccion' if cfg.env == 'production' else 'sandbox'}")

    async with httpx.AsyncClient(
        cert=(cfg.cert_path, cfg.key_path),
        timeout=120.0,
        headers={
            "Content-Type": "application/json",
            "Auth1": auth1,
            "Auth2": auth2,
        },
    ) as client:
        resp = await _post_with_failover(client, payload, cfg.env)

    print(f"\n<< Respuesta HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        print(f"  Body (no JSON): {resp.text[:500]}")
        return

    rc  = data.get("ResponseCode", "")
    iso = data.get("IsoCode", "")
    msg = data.get("ResponseMessage") or data.get("ErrorDescription") or ""
    tok = data.get("DataVaultToken", "")

    print(f"\n  ResponseCode:   {rc}")
    print(f"  IsoCode:        {iso}")
    print(f"  ResponseMessage:{msg}")
    print(f"  DataVaultToken: {tok!r}")
    print()

    if rc == "Error":
        err_desc = data.get("ErrorDescription", msg)
        if "DataVault" in err_desc:
            print("RESULTADO: DataVault NO esta habilitado para este merchant.")
            print(f"   Error AZUL: {err_desc}")
            print()
            print("   Solucion: Contactar a Luis Recio / AZUL Soluciones Integradas")
            print("     Telefono: 809-544-3659")
            print("     Correo:   solucionesintegradas@AZUL.com.do")
            print("     Solicitar: Activacion del modulo DataVault para merchant 39644300001")
        else:
            print(f"RESULTADO: Error de integracion -- {err_desc}")
    elif iso == "00" and tok:
        print(f"[OK] DataVault ACTIVO.")
        print(f"   Token recibido: {tok}")
    elif iso == "00" and not tok:
        print("[WARN] Sale aprobado (IsoCode=00) pero sin DataVaultToken.")
        print("   Esto puede indicar que DataVault aun no esta habilitado.")
    else:
        print(f"[WARN] Sale no aprobado (IsoCode={iso}) -- DataVault no pudo verificarse.")
        print(f"   Mensaje: {msg}")
        if tok:
            print(f"   Token: {tok}")

    print(f"\n  Raw response: {json.dumps(data, indent=2, ensure_ascii=False)}")


if __name__ == "__main__":
    asyncio.run(test_datavault())
