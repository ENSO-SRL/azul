"""
Azul Pagos Atlas — FastAPI application.

Run:
    uvicorn app.main:app --reload --port 8000

Swagger UI:
    http://localhost:8000/docs
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.infrastructure.database import init_db
from routers.health import router as health_router
from routers.payments import router as payments_router
from routers.recurring import router as recurring_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB tables.  Shutdown: nothing special."""
    await init_db()
    yield


app = FastAPI(
    title="Azul Pagos Atlas",
    description=(
        "Sistema de pagos integrado con Azul Payment Gateway.\n\n"
        "Soporta:\n"
        "- **Pagos únicos** (Sale)\n"
        "- **Pagos de servicios** (facturas, utilities)\n"
        "- **Pagos recurrentes** (suscripciones con DataVault)\n\n"
        "Usa el endpoint `/test/smoke` para validar la conexión con el sandbox de Azul."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(payments_router)
app.include_router(recurring_router)
