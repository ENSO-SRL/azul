"""Fix atlas_user_id NOT NULL constraint ."""
import asyncio
from app.infrastructure.database import engine
from sqlalchemy import text


async def fix():
    async with engine.begin() as conn:
        print("Aplicando ALTER TABLE pagos.payments...")

        await conn.execute(text(
            "ALTER TABLE pagos.payments ALTER COLUMN atlas_user_id DROP NOT NULL"
        ))
        print("  [OK] atlas_user_id -> nullable (pagos del checkout no tienen usuario Atlas)")

        await conn.execute(text(
            "ALTER TABLE pagos.payments ALTER COLUMN order_id SET DEFAULT ''"
        ))
        print("  [OK] order_id -> default vacio")

        print()
        print("Listo. La tabla pagos.payments ahora acepta inserts sin atlas_user_id.")


asyncio.run(fix())
