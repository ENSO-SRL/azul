#!/usr/bin/env python3
"""
simulate_full_flow.py — Simulación end-to-end del flujo Azul completo.

Requiere que el servidor esté corriendo en localhost:8000:
    uvicorn app.main:app --reload --port 8000

Flujo de 8 pasos:
    1. POST /api/v1/tokens        → crear token para CLI-42
    2. POST /api/v1/recurring     → suscripción mensual (primer cobro CIT)
    3. POST /api/v1/clubs/42/pay  → cobro CIT on-demand
    4. Forzar next_charge_at al pasado (directo en DB)
    5. GET  /test/scheduler/run   → disparar scheduler manualmente
    6. Verificar cobro MIT en BD
    7. DELETE /api/v1/tokens/{token} → eliminar tarjeta
    8. Verificar estado de la suscripción

Uso:
    python scripts/simulate_full_flow.py
    python scripts/simulate_full_flow.py --base-url http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"

# Tarjeta de prueba del sandbox Azul (Merchant 39038540035 — auth splitit)
CARD = {
    "card_number": "4260550061845872",
    "expiration": "203412",
    "cvc": "123",
    "cardholder_name": "CLI Simulacion",
    "cardholder_email": "simulacion@atlas.do",
}

CUSTOMER_ID = "CLI-SIM-42"
CLUB_ID = "42"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_step = 0


def step(title: str) -> None:
    global _step
    _step += 1
    print(f"\n{'='*60}")
    print(f"  Paso {_step}: {title}")
    print(f"{'='*60}")


def ok(label: str, value) -> None:
    print(f"  ✅ {label}: {json.dumps(value, ensure_ascii=False, default=str)}")


def fail(label: str, value) -> None:
    print(f"  ❌ {label}: {json.dumps(value, ensure_ascii=False, default=str)}")
    sys.exit(1)


def check(condition: bool, msg: str) -> None:
    if condition:
        ok("PASS", msg)
    else:
        fail("FAIL", msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(base_url: str) -> None:
    token_id = None
    token_value = None
    subscription_id = None

    with httpx.Client(base_url=base_url, timeout=130.0) as client:

        # ------------------------------------------------------------------
        # Paso 1: Crear token DataVault (sin cobrar)
        # ------------------------------------------------------------------
        step("Crear token DataVault (TrxType CREATE)")
        body = {
            "customer_id": CUSTOMER_ID,
            **CARD,
        }
        resp = client.post("/api/v1/tokens", json=body)
        print(f"  HTTP {resp.status_code}")

        if resp.status_code == 201:
            data = resp.json()
            ok("response", data)
            token_id = data["id"]
            token_value = data["token"]
            check(bool(token_value), f"DataVault token no vacío: {token_value}")
        elif resp.status_code == 503:
            print("  ⚠️  DataVault CREATE no habilitado en sandbox — usando sale+save_card para obtener token")
            # Fallback: sale con save_card=True
            sale_body = {
                "amount": 100,
                "itbis": 0,
                **CARD,
                "order_id": "sim-tokenize",
                "save_card": True,
            }
            sale_resp = client.post("/api/v1/payments", json=sale_body)
            if sale_resp.status_code == 200:
                sale_data = sale_resp.json()
                ok("sale fallback", sale_data)
                token_value = sale_data.get("data_vault_token", "")
                check(bool(token_value), f"token via sale: {token_value}")
                # List tokens to get token_id
                list_resp = client.get(f"/api/v1/tokens/{CUSTOMER_ID}")
                if list_resp.status_code == 200 and list_resp.json():
                    token_id = list_resp.json()[0]["id"]
            else:
                fail("sale fallback failed", sale_resp.json())
        else:
            fail("unexpected error", resp.json())

        # ------------------------------------------------------------------
        # Paso 2: Crear suscripción mensual (primer cobro CIT inmediato)
        # ------------------------------------------------------------------
        step("Crear suscripción mensual (primer cobro CIT + tokenización)")
        body = {
            "customer_id": CUSTOMER_ID,
            "amount": 5000,       # RD$50.00
            "itbis": 900,         # RD$9.00
            **{k: v for k, v in CARD.items()},
            "frequency_days": 30,
            "description": "Membresía mensual simulación",
        }
        resp = client.post("/api/v1/recurring", json=body)
        print(f"  HTTP {resp.status_code}")
        data = resp.json()
        ok("response", data)

        if resp.status_code == 200:
            subscription_id = data["id"]
            check(data["status"] == "ACTIVE", f"suscripción ACTIVE: {data['status']}")
            check(bool(data["data_vault_token"]), "token no vacío en suscripción")
            if not token_value:
                token_value = data["data_vault_token"]
        else:
            print("  ⚠️  Suscripción falló — continuando sin ID de suscripción")

        # ------------------------------------------------------------------
        # Paso 3: Cobro CIT on-demand (club)
        # ------------------------------------------------------------------
        step("Cobro CIT on-demand para club 42")
        if not token_value:
            print("  ⚠️  Sin token — saltando paso 3")
        else:
            body = {
                "customer_id": CUSTOMER_ID,
                "token": token_value,
                "amount": 10000,  # RD$100.00
                "itbis": 1800,
            }
            resp = client.post(f"/api/v1/clubs/{CLUB_ID}/pay", json=body)
            print(f"  HTTP {resp.status_code}")
            data = resp.json()
            ok("response", data)
            check(data.get("status") == "APPROVED", f"cobro club aprobado: {data.get('status')}")
            check(data.get("iso_code") == "00", f"IsoCode=00: {data.get('iso_code')}")

        # ------------------------------------------------------------------
        # Paso 4: Forzar next_charge_at al pasado (simulando que venció)
        # ------------------------------------------------------------------
        step("Forzar vencimiento de la suscripción (mutación directa en DB)")
        if not subscription_id:
            print("  ⚠️  Sin subscription_id — saltando pasos 4-6")
        else:
            try:
                import asyncio
                import sys
                import os
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

                async def force_due():
                    from sqlalchemy import update
                    from app.infrastructure.database import async_session_factory
                    from app.infrastructure.models import RecurringPaymentModel

                    past = datetime.now(timezone.utc) - timedelta(minutes=5)
                    async with async_session_factory() as session:
                        await session.execute(
                            update(RecurringPaymentModel)
                            .where(RecurringPaymentModel.id == subscription_id)
                            .values(next_charge_at=past)
                        )
                        await session.commit()
                    print(f"  ✅ next_charge_at forzado a: {past.isoformat()}")

                asyncio.run(force_due())
            except Exception as e:
                print(f"  ⚠️  No se pudo mutar DB directamente: {e}")
                print("       Ajusta manualmente next_charge_at en azul_pagos.db")

        # ------------------------------------------------------------------
        # Paso 5: Disparar scheduler manualmente
        # ------------------------------------------------------------------
        step("Disparar scheduler (GET /test/scheduler/run)")
        resp = client.get("/test/scheduler/run")
        print(f"  HTTP {resp.status_code}")
        data = resp.json()
        ok("response", data)
        check(data.get("status") == "ok", "scheduler respondió ok")

        # ------------------------------------------------------------------
        # Paso 6: Verificar cobro MIT en suscripción
        # ------------------------------------------------------------------
        step("Verificar estado de suscripción post-scheduler")
        if subscription_id:
            resp = client.get(f"/api/v1/recurring/{subscription_id}")
            print(f"  HTTP {resp.status_code}")
            data = resp.json()
            ok("suscripción", data)
            check(data["failed_attempts"] == 0, f"failed_attempts=0 (aprobada): {data['failed_attempts']}")
        else:
            print("  ⚠️  Sin subscription_id — saltando verificación")

        # ------------------------------------------------------------------
        # Paso 7: Eliminar tarjeta (DataVault DELETE)
        # ------------------------------------------------------------------
        step("Eliminar tarjeta de DataVault (TrxType DELETE)")
        if not token_value:
            print("  ⚠️  Sin token — saltando paso 7")
        else:
            resp = client.request(
                "DELETE",
                f"/api/v1/tokens/{token_value}",
                params={"customer_id": CUSTOMER_ID},
            )
            print(f"  HTTP {resp.status_code}")
            check(resp.status_code == 204, f"token eliminado (204): {resp.status_code}")

        # ------------------------------------------------------------------
        # Paso 8: Verificar tarjeta eliminada
        # ------------------------------------------------------------------
        step("Verificar que la tarjeta ya no aparece en la lista del cliente")
        resp = client.get(f"/api/v1/tokens/{CUSTOMER_ID}")
        print(f"  HTTP {resp.status_code}")
        cards = resp.json()
        ok("tarjetas restantes", cards)
        remaining_tokens = [c["token"] for c in cards if c.get("token") == token_value]
        check(len(remaining_tokens) == 0, "token eliminado de la lista local")

    # ------------------------------------------------------------------
    # Resumen
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("  SIMULACIÓN COMPLETADA")
    print(f"  Token creado:      {token_value}")
    print(f"  Suscripción ID:    {subscription_id}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulación end-to-end Azul")
    parser.add_argument(
        "--base-url",
        default=BASE_URL,
        help=f"URL base del servidor (default: {BASE_URL})",
    )
    args = parser.parse_args()
    main(args.base_url)
