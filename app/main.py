"""
Azul Pagos Atlas — FastAPI application.

Run:
    uvicorn app.main:app --reload --port 8000

Swagger UI:
    http://localhost:8000/docs
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.infrastructure.database import engine, init_db
from app.services.scheduler import run_now, start_scheduler, stop_scheduler
from routers.clubs import router as clubs_router
from routers.health import router as health_router
from routers.payments import router as payments_router
from routers.recurring import router as recurring_router
from routers.refunds import router as refunds_router
from routers.tests import router as tests_router
from routers.threeds import router as threeds_router
from routers.tokens import router as tokens_router


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
    version="0.4.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(payments_router)
app.include_router(refunds_router)
app.include_router(recurring_router)
app.include_router(tokens_router)
app.include_router(clubs_router)
app.include_router(threeds_router)
app.include_router(tests_router)


# ---------------------------------------------------------------------------
# Debug / test endpoints (sandbox only)
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
)
async def run_scheduler_now():
    """Manually trigger the subscription charging job."""
    azul_env = os.getenv("AZUL_ENV", "sandbox")
    if azul_env == "production":
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
