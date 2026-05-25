"""
Migración de emergencia: convierte idempotency_key='' -> NULL
en la tabla pagos.payments para resolver el UNIQUE constraint error.
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
        r = await conn.execute(
            text("SELECT COUNT(*) FROM pagos.payments WHERE idempotency_key = ''")
        )
        count = r.scalar()
        print(f"Rows with idempotency_key='': {count}")

        r2 = await conn.execute(
            text("UPDATE pagos.payments SET idempotency_key = NULL WHERE idempotency_key = ''")
        )
        print(f"Updated {r2.rowcount} rows to NULL")

    await engine.dispose()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(migrate())
