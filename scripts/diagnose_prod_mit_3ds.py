# -*- coding: utf-8 -*-
"""
Diagnóstico PRODUCCIÓN — ¿por qué fallan los cobros recurrentes (MIT)?
=====================================================================

Objetivo
--------
Validar al 100% la causa raíz: el cobro recurrente MIT en producción vuelve
`IsoCode=3D2METHOD` (3DS 2.0 forzado) porque `ForceNo3DS` se elimina en prod.
La pregunta que este script responde de forma definitiva es:

    ¿El comercio de producción ACEPTA `ForceNo3DS=1` en un MIT, o lo rechaza
    con `VALIDATION_ERROR:ForceNo3DS`?

Experimento
-----------
Con un DataVaultToken (tuyo), dispara el MISMO cobro MIT dos veces:

    A)  MIT SIN ForceNo3DS   → replica el comportamiento actual de prod.
    B)  MIT CON ForceNo3DS=1  → replica lo que se certificó en sandbox.

Árbol de decisión con el resultado:

  A=3D2METHOD  y  B=VALIDATION_ERROR:ForceNo3DS
      → CONFIRMADO: el comercio 39644300001 NO tiene habilitado el flujo
        recurrente/MIT-sin-3DS. Hay que pedírselo a AZUL (Luis Recio).
        Ningún cambio de código soluciona esto por sí solo.

  A=3D2METHOD  y  B=00 (APPROVED)
      → El único problema era nuestro strip de `_force_no3ds` en producción.
        Solución = revertir ese strip (enviar ForceNo3DS=1 en prod). No hace
        falta AZUL.

  A=00 (APPROVED)
      → El MIT ya funciona sin ForceNo3DS; el fallo estaba en otro lado
        (revisar credenciales/entorno del task que falla).

Obtener un token propio
-----------------------
Opción 1 (recomendada, NO genera cobro de tokenización):
    Pasá un DataVaultToken que ya exista para TU tarjeta con --token.
    (p.ej. el de tu propia suscripción: columna data_vault_token en
     pagos.recurring_payments).

Opción 2 (genera 1 cobro chico para tokenizar tu tarjeta):
    Pasá --card/--exp/--cvc y el script hará un Sale+SaveToDataVault para
    obtener el token. OJO: en prod `TrxType=CREATE` está deshabilitado, por
    eso se tokeniza cobrando. Si ese Sale también vuelve 3D2METHOD, el script
    lo reporta (dato adicional) y se detiene.

⚠️  DINERO REAL
---------------
Corre contra PRODUCCIÓN y usa una tarjeta real. El monto por defecto es
RD$1.00 (100 centavos). El cobro A (sin ForceNo3DS) normalmente NO cobra
(vuelve 3D2METHOD). El cobro B SÍ cobra si vuelve IsoCode=00. La tokenización
(opción 2) cobra el monto una vez.

Uso
---
    # 1) Confirmá primero qué entorno resuelve la config (no envía nada):
    python scripts/diagnose_prod_mit_3ds.py --check

    # 2) Con un token propio (sin cobro de tokenización):
    python scripts/diagnose_prod_mit_3ds.py --token <DATAVAULT_TOKEN> --yes-production

    # 2-bis) Tokenizando tu tarjeta (1 cobro chico):
    python scripts/diagnose_prod_mit_3ds.py \
        --card 4XXXXXXXXXXXXXXX --exp 203012 --cvc 123 \
        --amount 100 --name "Tu Nombre" --email tu@correo.com --yes-production
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

# stdout UTF-8 (tildes en Windows)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.entities import Currency, Payment, PaymentStatus, PaymentType
from app.infrastructure.azul_config import load_azul_config
from app.infrastructure.azul_gateway import (
    AzulIntegrationError,
    AzulPaymentGateway,
    _datavault_fields,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hr(title: str = "") -> None:
    print("\n" + "=" * 68)
    if title:
        print(f"  {title}")
        print("=" * 68)


def _mask(pan: str) -> str:
    if len(pan) < 10:
        return pan
    return pan[:6] + "*" * (len(pan) - 10) + pan[-4:]


def _new_payment(amount: int, itbis: int, name: str, email: str) -> Payment:
    """Payment con OrderNumber corto (<=15) y CustomOrderId único por intento."""
    short = uuid.uuid4().hex[:8].upper()
    return Payment(
        id=f"diag-mit-{short}",              # CustomOrderId único (evita idempotencia)
        order_id=f"DIAG{short[:6]}",         # OrderNumber corto y válido
        amount=amount,
        itbis=itbis,
        payment_type=PaymentType.RECURRING,
        auth_mode="splitit",
        initiated_by="merchant",
        currency_code=Currency.DOP,
        cardholder_name=name,
        cardholder_email=email,
    )


def _build_mit_payload(gw: AzulPaymentGateway, payment: Payment, token: str, force_no3ds: bool) -> dict:
    """Replica exactamente el payload de sale_mit(), con ForceNo3DS conmutrable."""
    payload = gw._base_payload(payment)
    payload.update({
        "CardNumber": "",
        "Expiration": "",
        "CVC": "",
        **_datavault_fields(False, token),
        "merchantInitiatedIndicator": "STANDING_ORDER",
    })
    if force_no3ds:
        payload["ForceNo3DS"] = "1"
    return payload


def _outcome(payment: Payment | None, error: str | None) -> str:
    """Etiqueta corta del resultado de un intento."""
    if error is not None:
        return f"ERROR:{error}"
    assert payment is not None
    iso = payment.iso_code or ""
    if iso == "00":
        return "APPROVED(00)"
    if iso in ("3D2METHOD", "3D"):
        return f"3DS_FORZADO({iso})"
    return f"{payment.status.value}(iso={iso or '∅'})"


async def _run_attempt(gw: AzulPaymentGateway, payment: Payment, token: str, force_no3ds: bool):
    """Ejecuta un MIT; devuelve (payment|None, error_str|None)."""
    payload = _build_mit_payload(gw, payment, token, force_no3ds)
    label = "CON ForceNo3DS=1" if force_no3ds else "SIN ForceNo3DS"
    _hr(f"INTENTO {'B' if force_no3ds else 'A'} — MIT {label}")
    print(f"  CustomOrderId : {payload.get('CustomOrderId')}")
    print(f"  OrderNumber   : {payload.get('OrderNumber')}")
    print(f"  DataVaultToken: {token[:8]}…{token[-4:]}")
    print(f"  ForceNo3DS enviado: {'sí' if force_no3ds else 'no'}")
    try:
        p, _txn = await gw._execute(payment, payload)
        print(f"  → ResponseCode = {p.response_code!r}")
        print(f"  → IsoCode      = {p.iso_code!r}")
        print(f"  → Mensaje      = {p.response_message!r}")
        print(f"  → AzulOrderId  = {p.azul_order_id!r}")
        print(f"  → Auth         = {p.authorization_code!r}")
        print(f"  → Estado       = {p.status.value}")
        if p.status == PaymentStatus.APPROVED:
            print("  ⚠️  ESTE INTENTO SÍ COBRÓ (IsoCode=00) — cobro real en tu tarjeta.")
        return p, None
    except AzulIntegrationError as e:
        # ResponseCode=Error → VALIDATION_ERROR:* (aquí cae ForceNo3DS rechazado)
        print(f"  → ResponseCode = Error")
        print(f"  → ErrorDescription = {e}")
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        print(f"  → EXCEPCIÓN: {type(e).__name__}: {e}")
        return None, f"{type(e).__name__}: {e}"


async def _tokenize(gw: AzulPaymentGateway, card: str, exp: str, cvc: str,
                    amount: int, itbis: int, name: str, email: str) -> str | None:
    """Opción 2: Sale+SaveToDataVault para obtener un token (genera 1 cobro)."""
    _hr("PASO 0 — Tokenizar tu tarjeta (Sale + SaveToDataVault=1)")
    print(f"  Tarjeta: {_mask(card)}   Monto: RD${amount/100:.2f}")
    print("  ⚠️  Esto genera UN cobro real en tu tarjeta.")
    p = _new_payment(amount, itbis, name, email)
    p.payment_type = PaymentType.SALE
    p.initiated_by = "cardholder"
    try:
        p, _txn = await gw.sale(p, card, exp, cvc, save_token=True)
    except AzulIntegrationError as e:
        print(f"  ✗ Falló la tokenización (integration error): {e}")
        return None
    print(f"  → IsoCode = {p.iso_code!r}   Estado = {p.status.value}")
    if p.iso_code in ("3D2METHOD", "3D"):
        print("  ✗ Hasta la tokenización (CIT con PAN) vuelve 3DS forzado en prod.")
        print("    → Dato adicional fuerte: el comercio exige 3DS incluso con tarjeta presente.")
        print("    → Usá --token con un token ya existente para completar el test MIT.")
        return None
    if p.status != PaymentStatus.APPROVED:
        print(f"  ✗ Tokenización no aprobada: {p.response_message}")
        return None
    token = p.data_vault_token or ""
    if not token:
        print("  ✗ Aprobó pero no devolvió DataVaultToken (DataVault no habilitado en el comercio).")
        return None
    print(f"  ✓ Token obtenido: {token[:8]}…{token[-4:]}")
    return token


def _verdict(a_outcome: str, b_outcome: str) -> None:
    _hr("VEREDICTO")
    print(f"  A (SIN ForceNo3DS) → {a_outcome}")
    print(f"  B (CON ForceNo3DS) → {b_outcome}")
    print()

    a_3ds = a_outcome.startswith("3DS_FORZADO")
    a_ok = a_outcome.startswith("APPROVED")
    b_ok = b_outcome.startswith("APPROVED")
    b_rejects_fn3ds = "ForceNo3DS" in b_outcome

    if a_ok:
        print("  ⇒ El MIT ya aprueba SIN ForceNo3DS. La causa raíz NO es 3DS en el")
        print("     comercio: revisá el task/credenciales del proceso que falla")
        print("     (posible deploy viejo con OrderNumber inválido).")
    elif a_3ds and b_rejects_fn3ds:
        print("  ⇒ CONFIRMADO 100%: el comercio de producción (39644300001) NO tiene")
        print("     habilitado el flujo recurrente/MIT sin 3DS. Rechaza ForceNo3DS y")
        print("     fuerza 3DS 2.0 en el MIT, que un cobro headless no puede completar.")
        print("     ► Acción: AZUL/Luis Recio debe habilitar MIT stored-credential sin")
        print("       3DS en ese merchant. Ningún cambio de código lo resuelve solo.")
    elif a_3ds and b_ok:
        print("  ⇒ El único bloqueante era nuestro strip de ForceNo3DS en producción.")
        print("     Con ForceNo3DS=1 el MIT aprueba. ► Acción: revertir el strip de")
        print("     `_force_no3ds` para producción y volver a desplegar. No hace falta AZUL.")
    else:
        print("  ⇒ Resultado no concluyente con el árbol esperado. Revisá los dos")
        print("     bloques de arriba (ResponseCode/IsoCode/ErrorDescription) y")
        print("     compartilos para afinar el diagnóstico.")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnóstico MIT/3DS en producción AZUL")
    ap.add_argument("--check", action="store_true",
                    help="Solo mostrar el entorno/merchant que resuelve la config. No envía nada.")
    ap.add_argument("--token", default="", help="DataVaultToken existente (tuyo). Evita cobro de tokenización.")
    ap.add_argument("--card", default="", help="PAN para tokenizar (opción 2 — genera 1 cobro).")
    ap.add_argument("--exp", default="", help="Expiración YYYYMM (p.ej. 203012).")
    ap.add_argument("--cvc", default="", help="CVC.")
    ap.add_argument("--amount", type=int, default=100, help="Monto en centavos (default 100 = RD$1.00).")
    ap.add_argument("--itbis", type=int, default=0, help="ITBIS en centavos (default 0).")
    ap.add_argument("--name", default="Diag MIT Test", help="CardHolderName.")
    ap.add_argument("--email", default="diag@atlas.do", help="CardHolderEmail.")
    ap.add_argument("--yes-production", action="store_true",
                    help="Confirmación obligatoria para enviar transacciones reales en producción.")
    ap.add_argument("--allow-sandbox", action="store_true",
                    help="Permitir correr aunque la config resuelva a sandbox (no es el test real).")
    args = ap.parse_args()

    # Logs de AZUL (request/response) visibles en stdout.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    cfg = load_azul_config()
    _hr("CONFIGURACIÓN RESUELTA")
    print(f"  AZUL_ENV     : {cfg.env}")
    print(f"  Merchant ID  : {cfg.merchant_id}")
    print(f"  API URL      : {cfg.api_url}")
    print(f"  Cert         : {cfg.cert_path}")
    print(f"  Fecha        : {datetime.now().isoformat(timespec='seconds')}")

    if args.check:
        print("\n  (modo --check: no se envía ninguna transacción)")
        return 0

    # Salvaguardas de entorno
    if cfg.env == "production" and not args.yes_production:
        print("\n  ✋ Es PRODUCCIÓN. Repetí el comando con --yes-production para enviar")
        print("     transacciones reales. (Podés usar --check para solo inspeccionar.)")
        return 2
    if cfg.env != "production" and not args.allow_sandbox:
        print(f"\n  ✋ La config resolvió a '{cfg.env}', no a producción. El test real debe")
        print("     correr contra el merchant 39644300001. Si querés probar igual acá,")
        print("     agregá --allow-sandbox (no valida la causa raíz de prod).")
        return 2

    gw = AzulPaymentGateway()

    # Resolver token
    token = args.token.strip()
    if not token:
        if not (args.card and args.exp and args.cvc):
            print("\n  ✋ Falta el token. Pasá --token <DATAVAULT_TOKEN> (recomendado) o")
            print("     --card/--exp/--cvc para tokenizar tu tarjeta (genera 1 cobro).")
            return 2
        token = await _tokenize(gw, args.card, args.exp, args.cvc,
                                args.amount, args.itbis, args.name, args.email)
        if not token:
            _hr("RESULTADO")
            print("  No se pudo obtener un token para el test MIT (ver arriba).")
            return 1

    # Experimento A/B (dos CustomOrderId distintos)
    pa = _new_payment(args.amount, args.itbis, args.name, args.email)
    a_payment, a_err = await _run_attempt(gw, pa, token, force_no3ds=False)

    pb = _new_payment(args.amount, args.itbis, args.name, args.email)
    b_payment, b_err = await _run_attempt(gw, pb, token, force_no3ds=True)

    _verdict(_outcome(a_payment, a_err), _outcome(b_payment, b_err))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
