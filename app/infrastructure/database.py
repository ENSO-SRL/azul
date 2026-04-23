"""
Async SQLite database setup via SQLAlchemy.
"""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Database URL — defaults to SQLite file in project root
# ---------------------------------------------------------------------------

_DB_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./azul_pagos.db")

engine = create_async_engine(_DB_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Base class for ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create all tables (called once on startup)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:  # type: ignore[misc]
    """FastAPI dependency — yields a session per request."""
    async with async_session() as session:
        yield session
