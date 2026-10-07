"""
Verificación de cobros en AZUL — ¿Están registrados en el comercio correcto?

Consulta VerifyPayment de AZUL para cada transacción indicada y muestra
si AZUL la tiene registrada (Found=true), con qué monto, IsoCode y
AuthorizationCode.

Uso:
    python scripts/verify_charges.py

    # Para verificar un CustomOrderId específico:
    python scripts/verify_charges.py --order diag-mit-E401BD68

    # Para verificar los últimos N pagos APPROVED de la DB local:
    python scripts/verify_charges.py --recent 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.azul_config import load_azul_config


# ═══════════════════════════════════════════════════════════════════════════════
# 23 transacciones verificadas en producción (2026-09-07)
# Todas: Found=true, IsoCode=00, Store=39644300001
# ═══════════════════════════════════════════════════════════════════════════════

# --- Cobros recurrentes MIT (scheduler automático) ---
MIT_RECURRING_ORDERS = [
    # sub-c7874293d3a2 | AzulOrder=370428955 | RD$500.00 | ITBIS=RD$90 | 44102800****7552 | Auth=262760 | RRN=20260905002627370428955 | 2026-09-05 00:26:25
    "sub-c7874293d3a2-c20260905-att1",
    # sub-383a6d5a76fd | AzulOrder=370397582 | RD$500.00 | ITBIS=RD$90 | 47416200****7335 | Auth=037800 | RRN=20260904222635370397582 | 2026-09-04 22:26:32
    "sub-383a6d5a76fd-c20260905-att3",
    # sub-b102b0e16b6a | AzulOrder=370397544 | RD$500.00 | ITBIS=RD$90 | 40025820****4936 | Auth=782017 | RRN=20260904222627370397544 | 2026-09-04 22:26:25
    "sub-b102b0e16b6a-c20260905-att3",
    # sub-a5cc61977ce8 | AzulOrder=370397563 | RD$500.00 | ITBIS=RD$90 | 51004116****9846 | Auth=263105 | RRN=20260904222632370397563 | 2026-09-04 22:26:28
    "sub-a5cc61977ce8-c20260905-att3",
    # sub-aa2a676c966b | AzulOrder=370298625 | RD$500.00 | ITBIS=RD$90 | 53421745****3332 | Auth=327623 | RRN=20260904192629370298625 | 2026-09-04 19:26:25
    "sub-aa2a676c966b-c20260904-att3",
    # sub-8b2e799f70e9 | AzulOrder=370569329 | RD$500.00 | ITBIS=RD$90 | 55231400****0329 | Auth=021161 | RRN=20260905102628370569329 | 2026-09-05 10:26:25
    "sub-8b2e799f70e9-c20260905-att3",
    # sub-4f319d0f7e08 | AzulOrder=370806245 | RD$2.00   | ITBIS=RD$0.36 | 46468911****1960 | Auth=262781 | RRN=20260905162628370806245 | 2026-09-05 16:26:25
    "sub-4f319d0f7e08-c20260905-att0",
    # 5339970e        | AzulOrder=364229390 | RD$500.00 | ITBIS=RD$90 | 46469011****7870 | Auth=111498 | RRN=20260820231115364229390 | 2026-08-20 23:10:53
    "5339970e-97da-4bc4-8392-c8bb6e7c9dc2",
]

# --- Ventas directas (Sale CIT) ---
DIRECT_SALE_ORDERS = [
    # c3f6f23c | AzulOrder=360662121 | RD$2.00 | ITBIS=RD$0.36 | 52273900****8269 | Auth=068640 | RRN=20260811090618360662121 | 2026-08-11 09:05:26
    "c3f6f23c-ed40-43ef-a107-9fc7fa793198",
    # 27b6cd9b | AzulOrder=361522500 | RD$2.00 | ITBIS=RD$0.36 | 55231400****5035 | Auth=079027 | RRN=20260813221921361522500 | 2026-08-13 22:18:33
    "27b6cd9b-56a3-4ef8-bfa1-795891ba3d1c",
]

# --- Verificaciones Hold (RD$1 — liberados vía Void) ---
HOLD_VERIFICATION_ORDERS = [
    # ceea29e4 | AzulOrder=369536238 | RD$1.00 | 52446300****7147 | Auth=052147 | RRN=20260903100334369536238 | 2026-09-03 10:03:12
    "ceea29e4-0004-47b9-bba5-4b05238310a2",
    # 0769705f | AzulOrder=361502057 | RD$1.00 | 55231400****5035 | Auth=069569 | RRN=20260813211117361502057 | 2026-08-13 21:10:12
    "0769705f-8abb-4f4c-b356-4c766b5d4c5d",
    # 2352f682 | AzulOrder=360418629 | RD$1.00 | 46468911****1960 | Auth=463854 | RRN=20260810144639360418629 | 2026-08-10 14:46:18
    "2352f682-a610-4528-ab12-c8e8a7205bf9",
    # cba10d94 | AzulOrder=361286213 | RD$1.00 | 46466900****2269 | Auth=063721 | RRN=20260813091604361286213 | 2026-08-13 09:15:43
    "cba10d94-523c-4fe8-b70a-f082ac3519ae",
    # c5112665 | AzulOrder=363916934 | RD$1.00 | 40025820****9022 | Auth=646928 | RRN=20260820090449363916934 | 2026-08-20 09:04:29
    "c5112665-3d34-4152-b66c-180d83a5369e",
    # 22e01047 | AzulOrder=366875599 | RD$1.00 | 44102800****7552 | Auth=315534 | RRN=20260827233156366875599 | 2026-08-27 23:31:36
    "22e01047-6608-4a07-a2a3-5470f39aa789",
    # 8193601a | AzulOrder=362953572 | RD$1.00 | 47416200****7335 | Auth=033357 | RRN=20260817190926362953572 | 2026-08-17 19:08:25
    "8193601a-9e81-4a55-a221-e303e9e6500d",
    # 8ee7151f | AzulOrder=363586676 | RD$1.00 | 54154703****0131 | Auth=593471 | RRN=20260819135808363586676 | 2026-08-19 13:57:55
    "8ee7151f-907c-4093-bad7-70c88ea0b820",
    # 5a1a06ee | AzulOrder=362938718 | RD$1.00 | 51004116****9846 | Auth=065722 | RRN=20260817180657362938718 | 2026-08-17 18:06:45
    "5a1a06ee-2c90-439c-8e7c-1de833005fb7",
    # 97907e9b | AzulOrder=363472499 | RD$1.00 | 40767558****9432 | Auth=002311 | RRN=20260819081425363472499 | 2026-08-19 08:14:04
    "97907e9b-e943-4106-b85a-2e4985924854",
    # 0edf1ebe | AzulOrder=362932862 | RD$1.00 | 40025820****4936 | Auth=358192 | RRN=20260817174315362932862 | 2026-08-17 17:42:55
    "0edf1ebe-3f5b-4bd1-8f52-1ac897706041",
    # acebc1d3 | AzulOrder=362923466 | RD$1.00 | 53421745****3332 | Auth=824467 | RRN=20260817170645362923466 | 2026-08-17 17:06:21
    "acebc1d3-3a6b-441f-902c-5231cb14337b",
    # 911c42cd | AzulOrder=363039387 | RD$1.00 | 55231400****0329 | Auth=072660 | RRN=20260818071329363039387 | 2026-08-18 07:12:30
    "911c42cd-af24-4f80-85f0-b7c3697b35a8",
]

# --- Diagnóstico MIT (prueba ForceNo3DS) ---
DIAGNOSTIC_ORDERS = [
    # MIT sin ForceNo3DS → 3D2METHOD (no cobró) | AzulOrder=369666132 | RRN= | 2026-09-03 15:54:55
    "diag-mit-7C10EF21",
    # MIT con ForceNo3DS=1 → APROBADA | AzulOrder=369666144 | Auth=937636 | RRN=20260903155501369666144 | 2026-09-03 15:54:58
    "diag-mit-E401BD68",
]

# All 23 + 2 diagnostic = 25 total
KNOWN_TEST_ORDERS = (
    MIT_RECURRING_ORDERS
    + DIRECT_SALE_ORDERS
    + HOLD_VERIFICATION_ORDERS
    + DIAGNOSTIC_ORDERS
)


async def verify_single(gw: AzulPaymentGateway, custom_order_id: str) -> dict:
    """Consulta VerifyPayment y retorna un resumen legible."""
    try:
        data = await gw.verify_payment(custom_order_id=custom_order_id)
        found = data.get("Found") in (True, "true", "True")
        return {
            "custom_order_id": custom_order_id,
            "found": found,
            "iso_code": data.get("IsoCode", ""),
            "amount": data.get("Amount", ""),
            "itbis": data.get("Itbis", ""),
            "authorization_code": data.get("AuthorizationCode", ""),
            "azul_order_id": data.get("AzulOrderId", data.get("AZULOrderId", "")),
            "rrn": data.get("RRN", ""),
            "lot_number": data.get("LotNumber", ""),
            "transaction_type": data.get("TransactionType", data.get("TrxType", "")),
            "response_message": data.get("ResponseMessage", ""),
            "date_time": data.get("DateTime", ""),
            "error": None,
            "raw": data,
        }
    except Exception as exc:
        return {
            "custom_order_id": custom_order_id,
            "found": False,
            "error": str(exc),
            "raw": {},
        }


async def verify_recent_from_db(gw: AzulPaymentGateway, limit: int = 10):
    """Lee los últimos pagos APPROVED de la DB local y los verifica en AZUL."""
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy import text

    db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///azul_pagos.db")
    engine = create_async_engine(db_url)

    results = []
    async with AsyncSession(engine) as session:
        query = text(
            "SELECT id, amount, iso_code, status, azul_order_id, authorization_code, "
            "created_at, payment_type "
            "FROM payments "
            "WHERE status = 'APPROVED' "
            "ORDER BY created_at DESC "
            f"LIMIT {limit}"
        )
        rows = (await session.execute(query)).fetchall()

        if not rows:
            print("\n⚠️  No hay pagos APPROVED en la base de datos local.")
            return []

        print(f"\n{'='*80}")
        print(f"  Verificando {len(rows)} pagos APPROVED recientes contra AZUL")
        print(f"{'='*80}\n")

        for row in rows:
            payment_id = row[0]
            local_amount = row[1]
            local_iso = row[2]
            local_status = row[3]
            local_azul_id = row[4]
            local_auth = row[5]
            created = row[6]
            ptype = row[7]

            result = await verify_single(gw, payment_id)
            result["local"] = {
                "amount": local_amount,
                "iso_code": local_iso,
                "status": local_status,
                "azul_order_id": local_azul_id,
                "authorization_code": local_auth,
                "created_at": str(created),
                "payment_type": ptype,
            }

            # Compare
            if result["found"]:
                match_amount = str(result.get("amount", "")) == str(local_amount)
                status_icon = "✅" if match_amount else "⚠️"
            else:
                status_icon = "❌"
                match_amount = False

            print(f"  {status_icon}  {payment_id}")
            print(f"      Tipo: {ptype} | Creado: {created}")
            print(f"      Local  → Amount={local_amount}, IsoCode={local_iso}, AuthCode={local_auth}")
            if result["found"]:
                print(f"      AZUL   → Amount={result.get('amount')}, IsoCode={result.get('iso_code')}, AuthCode={result.get('authorization_code')}")
                print(f"      AzulOrderId={result.get('azul_order_id')} | Lote={result.get('lot_number')} | RRN={result.get('rrn')}")
                if not match_amount:
                    print(f"      ⚠️  MISMATCH de monto: local={local_amount} vs AZUL={result.get('amount')}")
            elif result.get("error"):
                print(f"      ERROR → {result['error']}")
            else:
                print(f"      AZUL   → NOT FOUND (transacción no existe en AZUL con este CustomOrderId)")
            print()

            results.append(result)

    await engine.dispose()
    return results


async def main():
    parser = argparse.ArgumentParser(description="Verificar cobros en AZUL")
    parser.add_argument(
        "--order", "-o",
        help="CustomOrderId específico a verificar",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--recent", "-r",
        type=int,
        default=0,
        help="Verificar los últimos N pagos APPROVED de la DB local",
    )
    parser.add_argument(
        "--skip-known",
        action="store_true",
        help="No verificar las transacciones de diagnóstico conocidas",
    )
    args = parser.parse_args()

    cfg = load_azul_config()
    gw = AzulPaymentGateway()

    print(f"\n{'='*80}")
    print(f"  VERIFICACIÓN DE COBROS EN AZUL")
    print(f"{'='*80}")
    print(f"  Ambiente:    {cfg.env}")
    print(f"  Comercio:    {cfg.merchant_id}")
    print(f"  Verify URL:  {cfg.verify_payment_url}")
    print(f"  Fecha/hora:  {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*80}\n")

    # 1. Verificar transacciones de diagnóstico conocidas
    orders_to_check = []
    if not args.skip_known:
        orders_to_check.extend(KNOWN_TEST_ORDERS)
    orders_to_check.extend(args.order)

    if orders_to_check:
        print("─── Verificación por CustomOrderId ───\n")
        for order_id in orders_to_check:
            result = await verify_single(gw, order_id)
            if result["found"]:
                print(f"  ✅  {order_id}")
                print(f"      Found:     TRUE — AZUL tiene esta transacción registrada")
                print(f"      Amount:    {result.get('amount', '?')} centavos")
                print(f"      IsoCode:   {result.get('iso_code', '?')}")
                print(f"      AuthCode:  {result.get('authorization_code', '?')}")
                print(f"      AzulOrder: {result.get('azul_order_id', '?')}")
                print(f"      RRN:       {result.get('rrn', '?')}")
                print(f"      Lote:      {result.get('lot_number', '?')}")
                print(f"      Tipo:      {result.get('transaction_type', '?')}")
                print(f"      Fecha:     {result.get('date_time', '?')}")
                print(f"      Mensaje:   {result.get('response_message', '?')}")
            elif result.get("error"):
                print(f"  ❌  {order_id}")
                print(f"      ERROR: {result['error']}")
            else:
                print(f"  ❌  {order_id}")
                print(f"      Found:     FALSE — AZUL NO tiene esta transacción")
                print(f"      Mensaje:   {result.get('response_message', '?')}")
            print()

    # 2. Verificar pagos recientes de la DB
    if args.recent > 0:
        await verify_recent_from_db(gw, args.recent)

    # 3. Si no se pidió nada específico, verificar conocidos + últimos 5
    if not orders_to_check and args.recent == 0:
        print("─── Verificación de diagnósticos conocidos ───\n")
        for order_id in KNOWN_TEST_ORDERS:
            result = await verify_single(gw, order_id)
            if result["found"]:
                print(f"  ✅  {order_id}")
                print(f"      AZUL CONFIRMA: Amount={result.get('amount')}, "
                      f"IsoCode={result.get('iso_code')}, "
                      f"AuthCode={result.get('authorization_code')}, "
                      f"AzulOrderId={result.get('azul_order_id')}")
            else:
                print(f"  ❌  {order_id} → NOT FOUND")
            print()

        await verify_recent_from_db(gw, 5)

    # Summary
    print(f"\n{'='*80}")
    print("  RESUMEN")
    print(f"{'='*80}")
    print(f"  Store (MID):  {cfg.merchant_id}")
    print()
    print("  Si las transacciones aparecen con Found=TRUE e IsoCode=00,")
    print("  AZUL las tiene registradas bajo tu comercio.")
    print()
    print("  Para confirmar que el dinero llega a tu cuenta bancaria,")
    print("  contacta a AZUL con:")
    print(f"    → Store: {cfg.merchant_id}")
    print("    → AzulOrderId de cualquier transacción de arriba")
    print("    → Pregunta: '¿A qué cuenta bancaria está vinculado este Store?'")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    asyncio.run(main())
