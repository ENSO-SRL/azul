"""
Migración: Convierte el índice UNIQUE en pagos.payments.idempotency_key
para usar un índice parcial (excluye NULL y ''), permitiendo múltiples
pagos sin idempotency key.

Ejecutar UNA VEZ como migración de producción.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.infrastructure.azul_config import _load_dotenv
_load_dotenv()

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


async def migrate():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        # 1. Normalizar: convertir '' -> NULL en filas existentes
        r1 = await conn.execute(
            text("UPDATE pagos.payments SET idempotency_key = NULL WHERE idempotency_key = ''")
        )
        print(f"Normalized {r1.rowcount} rows: '' -> NULL")

        # 2. Verificar nombre del constraint actual
        r2 = await conn.execute(text("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'pagos'
              AND tablename = 'payments'
              AND indexname LIKE '%idempotency%'
        """))
        indexes = r2.fetchall()
        print(f"Current idempotency indexes: {indexes}")

        # 3. Drop UNIQUE constraint (which drops its backing index automatically)
        for idx_name, idx_def in indexes:
            constraint_name = idx_name  # the constraint has the same name as the index
            print(f"Dropping UNIQUE constraint: {constraint_name}")
            await conn.execute(text(
                f"ALTER TABLE pagos.payments DROP CONSTRAINT IF EXISTS {constraint_name}"
            ))

        # 4. Create partial UNIQUE index: only enforce uniqueness for non-empty keys
        await conn.execute(text("""
            CREATE UNIQUE INDEX payments_idempotency_key_partial
            ON pagos.payments (idempotency_key)
            WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''
        """))
        print("Created partial UNIQUE index on idempotency_key (excluding NULL and '')")

    await engine.dispose()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
