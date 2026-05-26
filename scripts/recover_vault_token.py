#!/usr/bin/env python3
"""
recover_vault_token.py — Recuperar o parchear un DataVault token faltante.

Caso de uso:
    Un pago fue APPROVED pero data_vault_token quedó vacío, impidiendo
    cobros recurrentes MIT futuros.

Estrategias (en orden):
    1. Consultar Azul vía VerifyPayment con el CustomOrderId (payment.id)
       para ver si Azul retorna el DataVaultToken en la respuesta.
    2. Si Azul no retorna token, mostrar instrucciones para tokenización
       manual (el usuario debe hacer un nuevo checkout con save_card=True).
    3. Modo --patch: escribir directamente un token conocido a la DB.

Uso:
    # Consultar si Azul tiene el token:
    python scripts/recover_vault_token.py --payment-id 8da2dbc3-fa16-4cfd-9d3f-98b13cfacff4

    # Parchear con un token conocido:
    python scripts/recover_vault_token.py \\
        --payment-id 8da2dbc3-fa16-4cfd-9d3f-98b13cfacff4 \\
        --patch-token <TOKEN_OBTENIDO_DE_AZUL>

    # También parchear en recurring_payments (suscripción):
    python scripts/recover_vault_token.py \\
        --payment-id 8da2dbc3-fa16-4cfd-9d3f-98b13cfacff4 \\
        --patch-token <TOKEN> \\
        --recurring-id <RECURRING_PAYMENT_UUID>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# ---------------------------------------------------------------------------
# Asegurar que el path del proyecto esté en sys.path
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Paso 1: Consultar Azul vía VerifyPayment
# ---------------------------------------------------------------------------

async def verify_with_azul(payment_id: str) -> dict:
    """Llama a VerifyPayment de Azul con el CustomOrderId = payment_id."""
    from app.infrastructure.azul_gateway import AzulPaymentGateway

    gw = AzulPaymentGateway()
    print(f"\n[AZUL] Consultando VerifyPayment con CustomOrderId={payment_id!r} ...")
    try:
        data = await gw.verify_payment(custom_order_id=payment_id)
        print(f"   Respuesta Azul: {json.dumps(data, indent=2, ensure_ascii=False)}")
        return data
    except Exception as exc:
        print(f"   [ERROR] Error al consultar Azul: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Paso 2: Leer el payment de la DB
# ---------------------------------------------------------------------------

async def get_payment(payment_id: str):
    """Recuperar el registro de payment de la base de datos."""
    from sqlalchemy import select
    from app.infrastructure.database import async_session
    from app.infrastructure.models import PaymentModel

    async with async_session() as session:
        result = await session.execute(
            select(PaymentModel).where(PaymentModel.id == payment_id)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Paso 3: Parchear token en payments (y opcionalmente recurring_payments)
# ---------------------------------------------------------------------------

async def patch_token(
    payment_id: str,
    token: str,
    recurring_id: str | None = None,
    customer_id: str | None = None,
    card_last4: str | None = None,
    card_brand: str | None = None,
    expiration: str | None = None,
) -> None:
    """Escribir el DataVaultToken en la DB sin tocar otros campos."""
    from sqlalchemy import update
    from app.infrastructure.database import async_session
    from app.infrastructure.models import PaymentModel, RecurringPaymentModel, SavedCardModel
    import uuid

    async with async_session() as session:

        # 1. Actualizar payments.data_vault_token
        result = await session.execute(
            update(PaymentModel)
            .where(PaymentModel.id == payment_id)
            .values(data_vault_token=token)
            .returning(PaymentModel.id)
        )
        updated = result.fetchone()
        if updated:
            print(f"   [OK] payments.data_vault_token actualizado -> {token!r}")
        else:
            print(f"   [WARN] No se encontro payment con id={payment_id!r}")

        # 2. Actualizar recurring_payments si se proveyó ID
        if recurring_id:
            rec_result = await session.execute(
                update(RecurringPaymentModel)
                .where(RecurringPaymentModel.id == recurring_id)
                .values(data_vault_token=token)
                .returning(RecurringPaymentModel.id)
            )
            rec_row = rec_result.fetchone()
            if rec_row:
                print(f"   [OK] recurring_payments.data_vault_token actualizado -> {token!r}")
            else:
                print(f"   [WARN] No se encontro recurring_payment con id={recurring_id!r}")

        # 3. Insertar en saved_cards si se proveyó customer_id
        if customer_id:
            saved = SavedCardModel(
                id=str(uuid.uuid4()),
                customer_id=customer_id,
                token=token,
                card_brand=card_brand or "",
                card_last4=card_last4 or "",
                expiration=expiration or "",
                is_default=True,
            )
            session.add(saved)
            print(f"   [OK] saved_cards: nuevo registro para customer_id={customer_id!r}")

        await session.commit()
        print("   [OK] Commit completado.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    payment_id = args.payment_id

    # ── Mostrar estado actual del payment ───────────────────────────────────
    print(f"\n{'='*65}")
    print(f"  recover_vault_token -- payment_id: {payment_id}")
    print(f"{'='*65}")

    payment = await get_payment(payment_id)
    if payment:
        token_status = "VACIO [NO OK]" if not payment.data_vault_token else "OK [PRESENTE]"
        print(f"\n[INFO] Payment actual en DB:")
        print(f"   id               : {payment.id}")
        print(f"   order_id         : {payment.order_id}")
        print(f"   azul_order_id    : {payment.azul_order_id}")
        print(f"   status           : {payment.status}")
        print(f"   cardholder_name  : {payment.cardholder_name}")
        print(f"   cardholder_email : {payment.cardholder_email}")
        print(f"   card_number_masked: {payment.card_number_masked}")
        print(f"   data_vault_token : {payment.data_vault_token!r}  <- {token_status}")
    else:
        print(f"\n   [ERROR] No se encontro payment con id={payment_id!r} en la DB local.")
        sys.exit(1)

    # ── Modo --patch ─────────────────────────────────────────────────────────
    if args.patch_token:
        print(f"\n[PATCH] Modo PATCH -- escribiendo token: {args.patch_token!r}")
        await patch_token(
            payment_id=payment_id,
            token=args.patch_token,
            recurring_id=args.recurring_id,
            customer_id=args.customer_id,
            card_last4=(payment.card_number_masked or "")[-4:] or None,
            card_brand=None,
            expiration=None,
        )
        print("\n[OK] Recuperacion completada. El scheduler ya puede usar este token para MIT.")
        return

    # ── Consultar Azul ───────────────────────────────────────────────────────
    azul_data = await verify_with_azul(payment_id)

    token_from_azul = azul_data.get("DataVaultToken", "")
    if token_from_azul:
        print(f"\n[OK] Azul retorno DataVaultToken: {token_from_azul!r}")
        print("   Aplicando automaticamente a la DB...")
        await patch_token(
            payment_id=payment_id,
            token=token_from_azul,
            recurring_id=args.recurring_id,
            customer_id=args.customer_id,
            card_last4=(payment.card_number_masked or "")[-4:] or None,
        )
        print("\n[OK] Recuperacion automatica completada.")
    else:
        print(f"""
[WARN] Azul NO retorno un DataVaultToken para este pago.

Esto ocurre porque:
  - El pago original fue procesado sin SaveToDataVault="1"  <- mas probable
  - DataVault no estaba habilitado en el momento del cobro

---------------------------------------------------------------
SOLUCION: El titular de la tarjeta (luis zadkiel duran) debe
          volver a ingresar su tarjeta terminada en 3831 a traves
          del checkout, esta vez con save_card=True.

Una vez obtenido el nuevo token, ejecutar:
  .venv\\Scripts\\python.exe scripts/recover_vault_token.py \\
      --payment-id {payment_id} \\
      --patch-token <NUEVO_TOKEN> \\
      --recurring-id <UUID_SUSCRIPCION> \\
      --customer-id <CUSTOMER_ID>
---------------------------------------------------------------
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Recuperar o parchear un DataVault token faltante en un payment APPROVED.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--payment-id",
        required=True,
        help="UUID del payment con token vacío (ej: 8da2dbc3-fa16-4cfd-9d3f-98b13cfacff4)",
    )
    parser.add_argument(
        "--patch-token",
        default=None,
        help="Token DataVault conocido para escribir directamente en DB (modo patch manual).",
    )
    parser.add_argument(
        "--recurring-id",
        default=None,
        help="UUID del recurring_payment asociado para actualizar también su token.",
    )
    parser.add_argument(
        "--customer-id",
        default=None,
        help="Customer ID para insertar también en saved_cards (ej: zad.duran@gmail.com).",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
