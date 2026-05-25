import asyncio, os, sys
sys.path.insert(0, r'C:\Users\zadkiel\Desktop\azul_pagos_atlas')
from app.infrastructure.azul_config import _load_dotenv; _load_dotenv()
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def check():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.connect() as c:
        r = await c.execute(text(
            "SELECT column_name, character_maximum_length, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema='pagos' AND table_name='payments' "
            "AND column_name IN ('status','threeds_method_form','threeds_challenge_form','threeds_redirect_url','response_message','iso_code') "
            "ORDER BY column_name"
        ))
        for row in r.fetchall():
            print(f"  {row[0]:<30} {row[2]}({row[1]})")
    await e.dispose()

asyncio.run(check())
