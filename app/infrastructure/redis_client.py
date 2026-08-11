"""
Redis client — async connection pool for Redis Cloud.

Configuración vía variables de entorno (ya existentes en ECS Task Definition):
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB, REDIS_SSL
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis.asyncio as aioredis  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
_REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
_REDIS_DB = int(os.getenv("REDIS_DB", "0"))
_REDIS_SSL = os.getenv("REDIS_SSL", "false").lower() in ("true", "1", "yes")

# Singleton del pool
_pool: aioredis.Redis | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def init_redis() -> None:
    """Inicializa el pool de conexiones a Redis. Llamar en startup."""
    global _pool
    if _pool is not None:
        return

    _pool = aioredis.Redis(
        host=_REDIS_HOST,
        port=_REDIS_PORT,
        password=_REDIS_PASSWORD,
        db=_REDIS_DB,
        ssl=_REDIS_SSL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )

    # Verificar conectividad
    try:
        await _pool.ping()
        logger.info(
            "[redis] Conexión establecida. host=%s port=%d db=%d ssl=%s",
            _REDIS_HOST, _REDIS_PORT, _REDIS_DB, _REDIS_SSL,
        )
    except Exception as e:
        logger.warning("[redis] No se pudo conectar a Redis: %s (el túnel no podrá leer resúmenes)", e)
        _pool = None


async def close_redis() -> None:
    """Cierra el pool de conexiones. Llamar en shutdown."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
        logger.info("[redis] Conexión cerrada.")


def get_redis() -> aioredis.Redis | None:
    """Retorna la instancia de Redis (o None si no se pudo conectar)."""
    return _pool


# ---------------------------------------------------------------------------
# Helpers específicos del Túnel
# ---------------------------------------------------------------------------

_TX_KEY_PREFIX = "atlas_card_transaction:"


async def get_transaction_summary(ref: str) -> dict[str, Any] | None:
    """Lee el resumen de transacción desde Redis.

    Clave: ``atlas_card_transaction:{ref}``
    Retorna el dict parseado o None si no existe / Redis no disponible.
    """
    if _pool is None:
        logger.warning("[redis] Pool no disponible — no se puede leer resumen para ref=%s", ref)
        return None

    try:
        raw = await _pool.get(f"{_TX_KEY_PREFIX}{ref}")
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.error("[redis] Error leyendo clave %s%s: %s", _TX_KEY_PREFIX, ref, e)
        return None


# ---------------------------------------------------------------------------
# 3DS Challenge Cache (Redis-backed, survives deploys)
# ---------------------------------------------------------------------------

_CHALLENGE_KEY_PREFIX = "atlas_3ds_challenge:"
_CHALLENGE_TTL_SECONDS = 300  # 5 minutos


async def store_challenge_form(payment_id: str, form_html: str) -> None:
    """Store a 3DS challenge form in Redis with a 5 minute TTL.

    Falls back silently if Redis is unavailable (the in-memory fallback
    in checkout.py still works for single-instance scenarios).
    """
    if _pool is None:
        return
    try:
        await _pool.setex(
            f"{_CHALLENGE_KEY_PREFIX}{payment_id}",
            _CHALLENGE_TTL_SECONDS,
            form_html,
        )
    except Exception as e:
        logger.warning("[redis] Failed to store challenge form for %s: %s", payment_id, e)


async def get_challenge_form(payment_id: str) -> str | None:
    """Retrieve and delete a 3DS challenge form from Redis.

    Uses GETDEL for atomic get+delete (one-time retrieval).
    Returns None if not found or Redis unavailable.
    """
    if _pool is None:
        return None
    try:
        result = await _pool.getdel(f"{_CHALLENGE_KEY_PREFIX}{payment_id}")
        return str(result) if result is not None else None
    except Exception as e:
        logger.warning("[redis] Failed to retrieve challenge form for %s: %s", payment_id, e)
        return None

