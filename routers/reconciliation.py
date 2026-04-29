"""
Reconciliation router.

Endpoints
---------
POST /api/v1/reconciliation/run            Lanzar reconciliación manualmente
GET  /api/v1/reconciliation/report         Ver último reporte
GET  /api/v1/reconciliation/mismatches     Ver solo las discrepancias
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_db
from app.services.reconciliation_service import ReconciliationService

router = APIRouter(prefix="/api/v1/reconciliation", tags=["Reconciliation"])


def _get_svc(db: AsyncSession = Depends(get_db)) -> ReconciliationService:
    return ReconciliationService(db)


@router.post(
    "/run",
    summary="Lanzar reconciliación manualmente",
    description=(
        "Corre la reconciliación contra AZUL para los pagos APPROVED del último día. "
        "Útil para ejecutar manualmente fuera del ciclo automático de medianoche. "
        "**Nota:** Consume cuota de verify_payment de AZUL — usar con moderación en producción."
    ),
)
async def run_reconciliation(
    days_back: int = Query(1, ge=1, le=30, description="Días hacia atrás a revisar"),
    svc: ReconciliationService = Depends(_get_svc),
):
    """Retorna un resumen con los conteos OK / MISMATCH / NOT_FOUND / ERROR."""
    summary = await svc.run(days_back=days_back)
    return {
        "status": "completed",
        "days_back": days_back,
        "summary": summary,
        "action_required": summary["mismatch"] > 0 or summary["not_found"] > 0,
    }


@router.get(
    "/report",
    summary="Ver reporte de reconciliación",
    description="Retorna las últimas N filas del reporte, opcionalmente filtradas por fecha.",
)
async def get_report(
    run_date: str | None = Query(None, description="Fecha YYYY-MM-DD (opcional)"),
    limit: int = Query(100, ge=1, le=500),
    svc: ReconciliationService = Depends(_get_svc),
):
    rows = await svc.get_report(run_date=run_date, limit=limit)
    return {"count": len(rows), "rows": rows}


@router.get(
    "/mismatches",
    summary="Ver solo discrepancias",
    description="Retorna únicamente las filas MISMATCH, NOT_FOUND y ERROR que requieren revisión manual.",
)
async def get_mismatches(
    run_date: str | None = Query(None, description="Fecha YYYY-MM-DD (opcional)"),
    svc: ReconciliationService = Depends(_get_svc),
):
    rows = await svc.get_mismatches(run_date=run_date)
    return {
        "count": len(rows),
        "action_required": len(rows) > 0,
        "rows": rows,
    }
