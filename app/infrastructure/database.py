"""
Async database setup — PostgreSQL via asyncpg + SQLAlchemy.

Configuración
-------------
Setea DATABASE_URL en el entorno o en .env:

    DATABASE_URL=postgresql+asyncpg://postgres:<password>@atlas-user-zadkiel-ohio.cna8kso8qh1g.us-east-2.rds.amazonaws.com:5432/Atlas_User_Service

Todas las tablas viven en el schema ``pagos`` (definido en models.py).
El schema debe existir antes de que la app arranque — crearlo con:

    CREATE SCHEMA IF NOT EXISTS pagos;

Pool de conexiones
------------------
asyncpg crea un pool de hasta 10 conexiones por worker.
En ECS con múltiples tareas, el total de conexiones = workers × 10.
Ajustar DB_POOL_SIZE según el max_connections de la RDS instance.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database URL — PostgreSQL (AWS RDS)
# ---------------------------------------------------------------------------

_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres@atlas-user-zadkiel-ohio.cna8kso8qh1g.us-east-2.rds.amazonaws.com:5432/Atlas_User_Service",
)

if not _DB_URL.startswith("postgresql"):
    raise RuntimeError(
        f"DATABASE_URL debe ser PostgreSQL (postgresql+asyncpg://...). "
        f"Valor actual: {_DB_URL!r}\n"
        "SQLite ya no está soportado — migrar a PostgreSQL."
    )

# Pool size — configurable vía env var para ajustar según RDS instance type
_POOL_SIZE     = int(os.getenv("DB_POOL_SIZE", "5"))
_MAX_OVERFLOW  = int(os.getenv("DB_MAX_OVERFLOW", "10"))
_POOL_TIMEOUT  = int(os.getenv("DB_POOL_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

engine = create_async_engine(
    _DB_URL,
    echo=False,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    pool_timeout=_POOL_TIMEOUT,
    pool_pre_ping=True,     # detecta conexiones muertas antes de usarlas
    pool_recycle=1800,      # recicla conexiones cada 30 min (evita timeouts de RDS)
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

logger.info(
    "[database] PostgreSQL engine inicializado. host=%s db=%s pool_size=%d",
    "atlas-user-zadkiel-ohio.cna8kso8qh1g.us-east-2.rds.amazonaws.com",
    "Atlas_User_Service",
    _POOL_SIZE,
)

# ---------------------------------------------------------------------------
# Base class for ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Crea todas las tablas en el schema pagos (llamado una vez en startup).

    En producción se prefiere gestionar el schema con migraciones SQL explícitas
    (el script infra/migration_pagos.sql). Esta función es útil para desarrollo
    y para garantizar que las tablas existen si el schema ya fue creado.
    """
    async with engine.begin() as conn:
        # Asegura que el schema existe antes de crear tablas
        await conn.execute(__import__("sqlalchemy").text("CREATE SCHEMA IF NOT EXISTS pagos"))
        await conn.run_sync(Base.metadata.create_all)
    logger.info("[database] init_db completado — tablas verificadas en schema pagos.")


async def get_db() -> AsyncSession:  # type: ignore[misc]
    """FastAPI dependency — yields a session per request."""
    async with async_session() as session:
        yield session
