import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/atlas_pagos")

async def main():
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT id, customer_id, token, card_brand, card_last4, expiration FROM pagos.saved_cards WHERE customer_id = '169'"))
        rows = res.fetchall()
        print(f"Cards for 169: {rows}")
        
        res = await conn.execute(text("SELECT id, status, payment_type, order_id, data_vault_token FROM pagos.payments WHERE customer_id = '169' ORDER BY created_at DESC LIMIT 5"))
        rows = res.fetchall()
        print(f"Payments for 169: {rows}")

asyncio.run(main())
