"""
API Key authentication — módulo de seguridad centralizado.

Uso:
    from app.security import require_api_key

    @router.post("/endpoint")
    async def my_endpoint(_: None = Depends(require_api_key)):
        ...

Configuración:
    API_KEY=<secret>  en variables de entorno / ECS Task Definition.

    En sandbox, si API_KEY no está definida, la autenticación se omite
    para facilitar el desarrollo local. En producción la variable es
    obligatoria — la app rechaza el arranque si no está configurada.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

# Header estándar para API keys
_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

# Leer la clave del entorno una sola vez al importar
_API_KEY: str | None = os.getenv("API_KEY")

_AZUL_ENV: str = os.getenv("AZUL_ENV", "sandbox")

# En producción la clave es obligatoria
if _AZUL_ENV == "production" and not _API_KEY:
    raise RuntimeError(
        "API_KEY environment variable is required in production. "
        "Set it in your ECS Task Definition or Parameter Store."
    )

if not _API_KEY:
    logger.warning(
        "[security] API_KEY not set — authentication DISABLED (sandbox/dev mode). "
        "Set API_KEY env var before deploying to production."
    )


async def require_api_key(api_key: str | None = Security(_API_KEY_HEADER)) -> None:
    """Dependency que exige el header X-API-Key en todos los endpoints protegidos.

    - Si API_KEY no está configurada (sandbox/dev) → permite todo el tráfico.
    - Si API_KEY está configurada → compara con timing-safe compare.
    - Si la clave es incorrecta → 401 Unauthorized.
    """
    if not _API_KEY:
        # Modo dev/sandbox sin autenticación configurada
        return

    if not api_key or not secrets.compare_digest(api_key, _API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey realm=\"Azul Pagos Atlas\""},
        )
