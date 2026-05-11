"""
Async database setup via SQLAlchemy.

Backends soportados
-------------------
- SQLite + aiosqlite (desarrollo / sandbox) — default
- SQLite + SQLCipher  (cifrado en reposo, temporal hasta migración a PostgreSQL)
- PostgreSQL + asyncpg (producción) — próxima migración

Cifrado en reposo (SQLite temporal)
------------------------------------
Si DATABASE_ENCRYPTION_KEY está definida, se aplica via SQLCipher:

    DATABASE_URL=sqlite+aiosqlite:///./azul_pagos.db
    DATABASE_ENCRYPTION_KEY=<clave_hex_32_bytes>

Generación de clave:
    python -c "import secrets; print(secrets.token_hex(32))"

Requisito (solo para SQLite cifrado):
    pip install sqlcipher3-binary   # o sqlcipher3 si hay compilador disponible

Migración a PostgreSQL (próxima fase):
    Basta con cambiar DATABASE_URL a postgresql+asyncpg://... y eliminar
    DATABASE_ENCRYPTION_KEY. El resto del código no cambia.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database URL — defaults to SQLite file in project root
# ---------------------------------------------------------------------------

_DB_URL           = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./azul_pagos.db")
_ENCRYPTION_KEY   = os.getenv("DATABASE_ENCRYPTION_KEY", "")

# ---------------------------------------------------------------------------
# Engine — con cifrado opcional via SQLCipher (Req 3)
# ---------------------------------------------------------------------------

def _build_engine():
    """Build the async engine, applying SQLCipher PRAGMA key if configured.

    Si DATABASE_ENCRYPTION_KEY está definida y el backend es SQLite,
    se inyecta el PRAGMA key en cada nueva conexión para activar el cifrado.

    TODO: Eliminar el bloque SQLCipher cuando se migre a PostgreSQL.
          Con PostgreSQL el cifrado en reposo se configura en la capa de
          almacenamiento (RDS encryption / pgcrypto para campos sensibles).
    """
    connect_args: dict[str, Any] = {}
    creator = None

    is_sqlite = _DB_URL.startswith("sqlite")

    if _ENCRYPTION_KEY and is_sqlite:
        # Aplicar PRAGMA key en cada conexión — activa SQLCipher
        key = _ENCRYPTION_KEY

        def _pragma_key(connection, connection_record):  # noqa: ARG001
            connection.execute(f"PRAGMA key='{key}'")

        from sqlalchemy import event

        _engine = create_async_engine(_DB_URL, echo=False, connect_args=connect_args)

        # Attach event listener BEFORE returning
        from sqlalchemy import event as _event

        @_event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):  # noqa: ARG001
            dbapi_conn.execute(f"PRAGMA key='{key}'")

        logger.info(
            "[database] SQLite encryption ENABLED via SQLCipher PRAGMA key. "
            "TODO: remove when migrating to PostgreSQL."
        )
        return _engine

    if is_sqlite and not _ENCRYPTION_KEY:
        logger.warning(
            "[database] SQLite running WITHOUT encryption (DATABASE_ENCRYPTION_KEY not set). "
            "Set DATABASE_ENCRYPTION_KEY or migrate to PostgreSQL for production use."
        )

    return create_async_engine(_DB_URL, echo=False, connect_args=connect_args)


engine = _build_engine()
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
