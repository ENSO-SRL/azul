"""
Migración: expande VARCHAR(20) → VARCHAR(50) en columnas de texto corto
de la tabla pagos.payments donde los valores del dominio superan el límite.

Afecta:
  - status       VARCHAR(20) → 50  (PENDING_3DS_CHALLENGE = 22 chars)
  - payment_type VARCHAR(20) → 50
  - auth_mode    VARCHAR(20) → 50
  - initiated_by VARCHAR(20) → 50
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.infrastructure.azul_config import _load_dotenv
_load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


COLUMNS_TO_EXPAND = [
    "status",
    "payment_type",
    "auth_mode",
    "initiated_by",
]


async def migrate():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        # Show current column lengths
        r = await conn.execute(text("""
            SELECT column_name, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = 'pagos'
              AND table_name   = 'payments'
              AND data_type    = 'character varying'
            ORDER BY column_name
        """))
        rows = r.fetchall()
        print("Current VARCHAR columns in pagos.payments:")
        for col, maxlen in rows:
            flag = " <- SHORT" if maxlen and maxlen < 50 and col in COLUMNS_TO_EXPAND else ""
            print(f"  {col:<35} VARCHAR({maxlen}){flag}")

        print()
        for col in COLUMNS_TO_EXPAND:
            print(f"  ALTER COLUMN {col} → VARCHAR(50) ...", end=" ")
            await conn.execute(text(
                f"ALTER TABLE pagos.payments ALTER COLUMN {col} TYPE VARCHAR(50)"
            ))
            print("OK")

    await engine.dispose()
    print("\nMigration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
