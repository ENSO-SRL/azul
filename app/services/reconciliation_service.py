"""
Reconciliation Service — cruza pagos locales vs AZUL.

Ejecuta un verify_payment para cada pago APPROVED de los últimos N días
y compara el resultado con lo que Atlas tiene en BD. Detecta discrepancias
y las persiste en la tabla reconciliation_reports.

Resultados posibles por fila
-----------------------------
OK          Atlas y AZUL coinciden en IsoCode y estado.
MISMATCH    Atlas dice APPROVED pero AZUL no lo encuentra o tiene IsoCode distinto.
NOT_FOUND   AZUL no encontró la transacción por CustomOrderId.
ERROR       Error de red o AZUL devolvió ResponseCode=Error.

Uso
---
    svc = ReconciliationService(session)
    summary = await svc.run(days_back=1)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.azul_gateway import AzulIntegrationError, AzulPaymentGateway
from app.infrastructure.models import PaymentModel, ReconciliationReportModel

logger = logging.getLogger(__name__)


class ReconciliationService:

    def __init__(self, session: AsyncSession):
        self._session = session
        self._gw = AzulPaymentGateway()

    async def run(self, days_back: int = 1) -> dict:
        """Run reconciliation for APPROVED payments in the last `days_back` days.

        Returns a summary dict with counts of OK / MISMATCH / NOT_FOUND / ERROR.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days_back)
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Fetch APPROVED payments from the last N days
        result = await self._session.execute(
            select(PaymentModel).where(
                PaymentModel.status == "APPROVED",
                PaymentModel.created_at >= since,
            )
        )
        payments = result.scalars().all()

        summary = {"total": len(payments), "ok": 0, "mismatch": 0, "not_found": 0, "error": 0}

        logger.info("[reconciliation] Starting run for %d payment(s) since %s", len(payments), since.date())

        for pm in payments:
            custom_order_id = pm.idempotency_key or pm.id
            row_status = "OK"
            azul_iso = ""
            azul_order_id = ""
            notes = ""

            try:
                data = await self._gw.verify_payment(custom_order_id)
                found = data.get("Found") in (True, "true", "True", 1)

                if not found:
                    row_status = "NOT_FOUND"
                    notes = f"AZUL did not find CustomOrderId={custom_order_id}"
                    summary["not_found"] += 1
                else:
                    azul_iso      = data.get("IsoCode", "")
                    azul_order_id = data.get("AzulOrderId", "")
                    local_iso     = pm.iso_code

                    if azul_iso != local_iso:
                        row_status = "MISMATCH"
                        notes = f"Local IsoCode={local_iso!r} vs AZUL IsoCode={azul_iso!r}"
                        summary["mismatch"] += 1
                        logger.warning("[reconciliation] MISMATCH payment=%s %s", pm.id, notes)
                    else:
                        summary["ok"] += 1

            except AzulIntegrationError as exc:
                row_status = "ERROR"
                notes = f"AzulIntegrationError: {exc}"
                summary["error"] += 1
                logger.error("[reconciliation] ERROR payment=%s: %s", pm.id, exc)

            except Exception as exc:
                row_status = "ERROR"
                notes = f"Unexpected: {exc}"
                summary["error"] += 1
                logger.exception("[reconciliation] Unexpected error payment=%s", pm.id)

            # Persist result row
            report = ReconciliationReportModel(
                id=str(uuid.uuid4()),
                run_date=run_date,
                payment_id=pm.id,
                custom_order_id=custom_order_id,
                local_status=pm.status,
                local_iso_code=pm.iso_code,
                azul_status="FOUND" if row_status != "NOT_FOUND" else "NOT_FOUND",
                azul_iso_code=azul_iso,
                azul_order_id=azul_order_id,
                status=row_status,
                notes=notes[:500],
            )
            self._session.add(report)

        await self._session.commit()
        logger.info("[reconciliation] Done — %s", summary)
        return summary

    async def get_report(self, run_date: str | None = None, limit: int = 200) -> list[dict]:
        """Return reconciliation rows, optionally filtered by run_date (YYYY-MM-DD)."""
        q = select(ReconciliationReportModel).order_by(
            ReconciliationReportModel.checked_at.desc()
        ).limit(limit)
        if run_date:
            q = q.where(ReconciliationReportModel.run_date == run_date)

        result = await self._session.execute(q)
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "run_date": r.run_date,
                "payment_id": r.payment_id,
                "custom_order_id": r.custom_order_id,
                "local_status": r.local_status,
                "local_iso_code": r.local_iso_code,
                "azul_status": r.azul_status,
                "azul_iso_code": r.azul_iso_code,
                "azul_order_id": r.azul_order_id,
                "status": r.status,
                "notes": r.notes,
                "checked_at": r.checked_at.isoformat(),
            }
            for r in rows
        ]

    async def get_mismatches(self, run_date: str | None = None) -> list[dict]:
        """Return only MISMATCH and NOT_FOUND rows — what needs human review."""
        all_rows = await self.get_report(run_date=run_date)
        return [r for r in all_rows if r["status"] in ("MISMATCH", "NOT_FOUND", "ERROR")]
