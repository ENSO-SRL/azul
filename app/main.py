"""
Azul Pagos Atlas — FastAPI application.

Run:
    uvicorn app.main:app --reload --port 8000

Swagger UI:
    http://localhost:8000/docs  (solo disponible fuera de producción)
"""

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.infrastructure.database import engine, init_db
from app.security import require_api_key
from app.services.scheduler import run_now, start_scheduler, stop_scheduler
from routers.clubs import router as clubs_router
from routers.health import router as health_router
from routers.notifications import router as notifications_router
from routers.payments import router as payments_router
from routers.reconciliation import router as reconciliation_router
from routers.recurring import router as recurring_router
from routers.refunds import router as refunds_router
from routers.tests import router as tests_router
from routers.threeds import router as threeds_router
from routers.tokens import router as tokens_router
from app.routers.cert import router as cert_router

logger = logging.getLogger(__name__)

_AZUL_ENV = os.getenv("AZUL_ENV", "sandbox")

# ---------------------------------------------------------------------------
# Swagger UI — deshabilitado en producción (Req 6+7)
# ---------------------------------------------------------------------------
_docs_url    = None if _AZUL_ENV == "production" else "/docs"
_redoc_url   = None if _AZUL_ENV == "production" else "/redoc"
_openapi_url = None if _AZUL_ENV == "production" else "/openapi.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB tables + start scheduler.  Shutdown: stop scheduler."""
    await init_db()
    start_scheduler(engine)
    yield
    stop_scheduler()


app = FastAPI(
    title="Azul Pagos Atlas",
    description=(
        "Sistema de pagos integrado con Azul Payment Gateway.\n\n"
        "Soporta:\n"
        "- **Pagos únicos** (Sale CIT con tarjeta completa)\n"
        "- **Pagos de servicios** (facturas, utilities)\n"
        "- **Pagos de clubs** (CIT on-demand con token DataVault)\n"
        "- **Pagos recurrentes** (suscripciones MIT con DataVault)\n"
        "- **Tokenización** (DataVault CREATE / DELETE)\n"
        "- **Cancelaciones** (Void ≤20 min | Refund >20 min)\n"
        "- **3DS 2.0** (autenticación 3D Secure para pagos CIT)\n\n"
        "### Autenticación\n"
        "Todos los endpoints (excepto `/health` y `/`) requieren el header:\n"
        "`X-API-Key: <tu_clave>`\n\n"
        "### 3DS 2.0\n"
        "El flujo 3DS se activa con `auth_mode=\"3dsecure\"` y `browser_info`. "
        "El pago puede pasar por estados `PENDING_3DS_METHOD` y "
        "`PENDING_3DS_CHALLENGE` antes de finalizar.\n\n"
        "### Idempotencia\n"
        "Pasa el header `Idempotency-Key` en cualquier endpoint de cobro para "
        "reintentos seguros sin cobros duplicados.\n\n"
        "### Smoke test\n"
        "Usa `/test/smoke` para validar mTLS + autenticación contra el sandbox de Azul."
    ),
    version="0.5.0",
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
)


# ---------------------------------------------------------------------------
# Middleware — audit logging por request (Req 10)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def audit_log_middleware(request: Request, call_next) -> Response:
    """Log every request with method, path, client IP, status, and latency."""
    start = time.perf_counter()
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    response: Response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "[audit] %s %s | ip=%s status=%d %.1fms",
        request.method,
        request.url.path,
        client_ip,
        response.status_code,
        elapsed_ms,
    )
    return response


# ---------------------------------------------------------------------------
# Routers — health y 3DS callbacks son públicos; el resto requiere API Key
# ---------------------------------------------------------------------------

# Públicos: health checks, callbacks ACS y dashboard de certificación
app.include_router(health_router)
app.include_router(threeds_router)          # /method-notification y /term son callbacks del ACS
app.include_router(cert_router)             # /cert  — dashboard interactivo de certificación

# Protegidos con X-API-Key
_auth = [Depends(require_api_key)]
app.include_router(payments_router,       dependencies=_auth)
app.include_router(refunds_router,        dependencies=_auth)
app.include_router(recurring_router,      dependencies=_auth)
app.include_router(tokens_router,         dependencies=_auth)
app.include_router(clubs_router,          dependencies=_auth)
app.include_router(tests_router,          dependencies=_auth)
app.include_router(notifications_router,  dependencies=_auth)
app.include_router(reconciliation_router, dependencies=_auth)


# ---------------------------------------------------------------------------
# Debug / test endpoints (sandbox only) — protegidos con API Key
# ---------------------------------------------------------------------------

@app.get(
    "/test/scheduler/run",
    tags=["Debug"],
    summary="Disparar scheduler manualmente (sandbox only)",
    description=(
        "Ejecuta inmediatamente el job de cobro de suscripciones vencidas. "
        "Solo disponible cuando `AZUL_ENV=sandbox`. "
        "Útil para pruebas de integración sin esperar el cron de 1 hora."
    ),
    dependencies=[Depends(require_api_key)],
)
async def run_scheduler_now():
    """Manually trigger the subscription charging job."""
    if _AZUL_ENV == "production":
        raise HTTPException(
            status_code=403,
            detail="Debug endpoint not available in production",
        )
    count = await run_now(engine)
    return {
        "status": "ok",
        "subscriptions_processed": count,
        "message": f"Charged {count} due subscription(s)",
    }
