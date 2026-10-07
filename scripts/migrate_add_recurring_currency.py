"""
Migración: agrega la columna currency_code a pagos.recurring_payments.

Antes de este cambio la moneda de la suscripción no se persistía y todo cobro
recurrente usaba DOP por defecto. Esta columna almacena la moneda ISO (DOP|USD)
para que cada cobro MIT use la moneda correcta.

Idempotente: usa ADD COLUMN IF NOT EXISTS. Las filas existentes reciben 'DOP'
vía DEFAULT/backfill.

Uso:
    DATABASE_URL=postgresql+asyncpg://... python scripts/migrate_add_recurring_currency.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.infrastructure.azul_config import _load_dotenv
_load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


async def migrate() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        # Estado actual
        r = await conn.execute(text("""
            SELECT column_name, data_type, character_maximum_length, column_default
            FROM information_schema.columns
            WHERE table_schema = 'pagos'
              AND table_name   = 'recurring_payments'
              AND column_name  = 'currency_code'
        """))
        if r.fetchone():
            print("Columna currency_code ya existe — nada que hacer.")
        else:
            print("Agregando pagos.recurring_payments.currency_code VARCHAR(3) DEFAULT 'DOP' ...", end=" ")
            await conn.execute(text(
                "ALTER TABLE pagos.recurring_payments "
                "ADD COLUMN IF NOT EXISTS currency_code VARCHAR(3) NOT NULL DEFAULT 'DOP'"
            ))
            print("OK")

        # Backfill defensivo por si la columna existía como NULLable
        updated = await conn.execute(text(
            "UPDATE pagos.recurring_payments SET currency_code = 'DOP' "
            "WHERE currency_code IS NULL OR currency_code = ''"
        ))
        print(f"Backfill filas a 'DOP': {updated.rowcount}")

    await engine.dispose()
    print("\nMigración completa.")


if __name__ == "__main__":
    asyncio.run(migrate())
