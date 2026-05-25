import asyncio, os, sys
sys.path.insert(0, r'C:\Users\zadkiel\Desktop\azul_pagos_atlas')
from app.infrastructure.azul_config import _load_dotenv; _load_dotenv()
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def check():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.connect() as c:
        r = await c.execute(text(
            "SELECT id, order_id, amount, itbis, status, iso_code, response_code, "
            "response_message, azul_order_id, authorization_code, created_at "
            "FROM pagos.payments "
            "ORDER BY created_at DESC LIMIT 10"
        ))
        rows = r.fetchall()
        print(f"{'ID':<38} {'order_id':<16} {'amount':>6} {'status':<25} {'iso':<12} {'auth_code':<15} {'created_at'}")
        print("-" * 130)
        for row in rows:
            total = (row[2] + row[3]) / 100
            print(f"{row[0]:<38} {row[1]:<16} {total:>6.2f} {row[4]:<25} {row[5]:<12} {str(row[9]):<15} {row[10]}")
    await e.dispose()

asyncio.run(check())
