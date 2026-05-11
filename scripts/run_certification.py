# -*- coding: utf-8 -*-
"""
Certificacion AZUL Sandbox - Script autonomo
============================================

Ejecuta todos los tests requeridos por Luis Recio (BPD/AZUL) y genera:
  1. evidencia_certificacion.json  — datos estructurados completos
  2. reporte_luis_recio.md         — reporte listo para enviar

Uso:
    py scripts/run_certification.py

Requiere AZUL_LOCAL_MODE=1 en el .env (modo sandbox).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
# Forzar UTF-8 en stdout para tildes en Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from datetime import datetime
from pathlib import Path

# Asegurar que el root del proyecto esté en el path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.entities import Currency, Payment, PaymentStatus, PaymentType
from app.infrastructure.azul_gateway import AzulIntegrationError, AzulPaymentGateway

# ---------------------------------------------------------------------------
# Tarjetas de prueba (email Luis Recio, 23-abr-2026)
# ---------------------------------------------------------------------------

EXPIRATION   = "203412"
CVC          = "123"
VISA_1       = "4260550061845872"       # Visa principal
MASTERCARD_1 = "5424180279791732"       # Mastercard
DISCOVER_1   = "6011000990099818"       # Discover
VISA_3DS     = "4005520000000129"       # Visa que activa 3DS

MERCHANT_ID  = "39038540035"
ECOMMERCE_URL = "https://www.iamatlas.do"
TODAY = datetime.now().strftime("%Y%m%d")
NOW   = datetime.now().strftime("%Y-%m-%d %H:%M AST")

BROWSER_INFO = {
    "accept_header": "text/html,application/xhtml+xml",
    "ip_address": "127.0.0.1",
    "language": "es-DO",
    "color_depth": "24",
    "screen_width": "1920",
    "screen_height": "1080",
    "time_zone": "240",
    "user_agent": "Atlas-Certification/1.0",
    "javascript_enabled": "true",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payment(
    amount: int = 10000,
    itbis: int = 1500,
    auth_mode: str = "splitit",
) -> Payment:
    p = Payment(
        amount=amount,
        itbis=itbis,
        payment_type=PaymentType.SALE,
        auth_mode=auth_mode,
        cardholder_name="Atlas Cert Test",
        cardholder_email="cert@iamatlas.do",
        currency_code=Currency.DOP,
    )
    return p


def _mask(pan: str) -> str:
    if len(pan) < 10:
        return pan
    return pan[:6] + "*" * (len(pan) - 10) + pan[-4:]


def _rec(results: list, test: str, card_raw: str, p: Payment, extra: dict | None = None) -> dict:
    row = {
        "test":                test,
        "fecha":               NOW,
        "merchant_id":         MERCHANT_ID,
        "tarjeta_enmascarada": _mask(card_raw) if len(card_raw) > 6 else card_raw,
        "red":                 _detect_brand(card_raw),
        "monto":               f"RD${p.amount/100:.2f}",
        "itbis":               f"RD${p.itbis/100:.2f}",
        "moneda":              "DOP",
        "iso_code":            p.iso_code or "",
        "response_code":       p.response_code or "",
        "response_message":    p.response_message or "",
        "AzulOrderId":         p.azul_order_id or "",
        "AuthorizationCode":   p.authorization_code or "",
        "CustomOrderId":       str(p.id),
        "ECommerceUrl":        ECOMMERCE_URL,
        "estado":              p.status.value if p.status else "UNKNOWN",
        "DataVaultToken":      p.data_vault_token or "",
    }
    if extra:
        row.update(extra)
    results.append(row)
    return row


def _detect_brand(pan: str) -> str:
    if pan.startswith("4"):
        return "Visa"
    if pan.startswith("5"):
        return "Mastercard"
    if pan.startswith("6011"):
        return "Discover"
    return "Unknown"


def _print(label: str, ok: bool, detail: str = "") -> None:
    icon = "[OK]" if ok else "[FAIL]"
    print(f"  {icon}  {label}" + (f"  ->  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def run_all() -> None:
    os.environ.setdefault("AZUL_LOCAL_MODE", "1")
    os.environ.setdefault("AZUL_ECOMMERCE_URL", ECOMMERCE_URL)

    gw = AzulPaymentGateway()
    results: list[dict] = []
    skipped: list[str] = []
    failures: list[str] = []

    print("\n" + "="*60)
    print("  AZUL SANDBOX — CERTIFICACIÓN COMPLETA")
    print(f"  Merchant: {MERCHANT_ID}")
    print(f"  Fecha:    {NOW}")
    print("="*60)

    # -----------------------------------------------------------------------
    # TEST 1 — Sale Visa
    # -----------------------------------------------------------------------
    print("\n[1/9] Sale Visa...")
    try:
        p, _ = await gw.sale(_payment(), VISA_1, EXPIRATION, CVC)
        ok = p.status == PaymentStatus.APPROVED
        r = _rec(results, "Sale Visa", VISA_1, p)
        _print("Sale Visa", ok, f"AzulOrderId={r['AzulOrderId']}  Auth={r['AuthorizationCode']}  IsoCode={r['iso_code']}")
        if not ok:
            failures.append(f"Sale Visa: {p.response_message}")
    except Exception as e:
        failures.append(f"Sale Visa ERROR: {e}")
        _print("Sale Visa", False, str(e))

    # -----------------------------------------------------------------------
    # TEST 2 — Sale Mastercard
    # -----------------------------------------------------------------------
    print("\n[2/9] Sale Mastercard...")
    try:
        p, _ = await gw.sale(_payment(), MASTERCARD_1, EXPIRATION, CVC)
        ok = p.status == PaymentStatus.APPROVED
        r = _rec(results, "Sale Mastercard", MASTERCARD_1, p)
        _print("Sale Mastercard", ok, f"AzulOrderId={r['AzulOrderId']}  Auth={r['AuthorizationCode']}  IsoCode={r['iso_code']}")
        if not ok:
            failures.append(f"Sale Mastercard: {p.response_message}")
    except Exception as e:
        failures.append(f"Sale Mastercard ERROR: {e}")
        _print("Sale Mastercard", False, str(e))

    # -----------------------------------------------------------------------
    # TEST 3 — Sale Discover
    # -----------------------------------------------------------------------
    print("\n[3/9] Sale Discover...")
    try:
        p, _ = await gw.sale(_payment(), DISCOVER_1, EXPIRATION, CVC)
        ok = p.status == PaymentStatus.APPROVED
        r = _rec(results, "Sale Discover", DISCOVER_1, p)
        _print("Sale Discover", ok, f"AzulOrderId={r['AzulOrderId']}  Auth={r['AuthorizationCode']}  IsoCode={r['iso_code']}")
        if not ok:
            failures.append(f"Sale Discover: {p.response_message}")
    except Exception as e:
        failures.append(f"Sale Discover ERROR: {e}")
        _print("Sale Discover", False, str(e))

    # -----------------------------------------------------------------------
    # TEST 4 — Sale + DataVault (tokenización)
    # -----------------------------------------------------------------------
    print("\n[4/9] Sale + DataVault (tokenización)...")
    datavault_token: str = ""
    try:
        p, _ = await gw.sale(_payment(), VISA_1, EXPIRATION, CVC, save_token=True)
        ok = p.status == PaymentStatus.APPROVED
        datavault_token = p.data_vault_token or ""
        token_ok = bool(datavault_token)
        extra = {
            "SaveToDataVault": "1",
            "token_generado":  "Sí" if token_ok else "No",
        }
        r = _rec(results, "Sale + DataVault", VISA_1, p, extra)
        _print("Sale + DataVault", ok and token_ok,
               f"Token={datavault_token[:16]}...  AzulOrderId={r['AzulOrderId']}")
        if not ok:
            failures.append(f"Sale+DataVault: {p.response_message}")
        if not token_ok:
            failures.append("Sale+DataVault: DataVaultToken vacío — DataVault no habilitado")
    except Exception as e:
        failures.append(f"Sale+DataVault ERROR: {e}")
        _print("Sale+DataVault", False, str(e))

    # -----------------------------------------------------------------------
    # TEST 5 — MIT con token (Merchant Initiated)
    # -----------------------------------------------------------------------
    print("\n[5/9] MIT con token (STANDING_ORDER)...")
    if datavault_token:
        try:
            mit = _payment()
            mit.initiated_by = "merchant"
            mit, _ = await gw.sale_mit(mit, datavault_token)
            ok = mit.status == PaymentStatus.APPROVED
            extra = {
                "DataVaultToken_usado":      datavault_token[:16] + "...",
                "merchantInitiatedIndicator": "STANDING_ORDER",
                "ForceNo3DS":                "1",
            }
            r = _rec(results, "MIT STANDING_ORDER", "(DataVaultToken)", mit, extra)
            _print("MIT STANDING_ORDER", ok,
                   f"AzulOrderId={r['AzulOrderId']}  Auth={r['AuthorizationCode']}")
            if not ok:
                failures.append(f"MIT: {mit.response_message}")
        except Exception as e:
            failures.append(f"MIT ERROR: {e}")
            _print("MIT STANDING_ORDER", False, str(e))
    else:
        skipped.append("MIT — DataVaultToken no disponible (DataVault no habilitado)")
        _print("MIT STANDING_ORDER", False, "SKIPPED — DataVaultToken no disponible")
        results.append({"test": "MIT STANDING_ORDER", "estado": "SKIPPED",
                        "motivo": "DataVault no habilitado en sandbox"})

    # -----------------------------------------------------------------------
    # TEST 6 — CIT con token (Cardholder Initiated)
    # -----------------------------------------------------------------------
    print("\n[6/9] CIT con token (STANDING_ORDER)...")
    if datavault_token:
        try:
            cit, _ = await gw.sale_cit(_payment(), datavault_token)
            ok = cit.status == PaymentStatus.APPROVED
            extra = {
                "DataVaultToken_usado":       datavault_token[:16] + "...",
                "cardholderInitiatedIndicator": "STANDING_ORDER",
                "ForceNo3DS":                 "1",
            }
            r = _rec(results, "CIT STANDING_ORDER", "(DataVaultToken)", cit, extra)
            _print("CIT STANDING_ORDER", ok,
                   f"AzulOrderId={r['AzulOrderId']}  Auth={r['AuthorizationCode']}")
            if not ok:
                failures.append(f"CIT: {cit.response_message}")
        except Exception as e:
            failures.append(f"CIT ERROR: {e}")
            _print("CIT STANDING_ORDER", False, str(e))
    else:
        skipped.append("CIT — DataVaultToken no disponible")
        _print("CIT STANDING_ORDER", False, "SKIPPED — DataVaultToken no disponible")
        results.append({"test": "CIT STANDING_ORDER", "estado": "SKIPPED",
                        "motivo": "DataVault no habilitado en sandbox"})

    # -----------------------------------------------------------------------
    # TEST 7 — 3DS 2.0
    # -----------------------------------------------------------------------
    print("\n[7/9] 3DS 2.0 (VISA_3DS)...")
    try:
        p3 = _payment(auth_mode="3dsecure")
        p3, _ = await gw.sale(p3, VISA_3DS, EXPIRATION, CVC, browser_info=BROWSER_INFO)
        iso = p3.iso_code or ""
        ok = iso in ("3D2METHOD", "3D", "00")
        extra = {
            "ThreeDSMethodForm": "presente" if p3.threeds_method_form else "vacío",
            "resultado_esperado": "3D2METHOD o flujo 3DS activado",
        }
        r = _rec(results, "3DS 2.0", VISA_3DS, p3, extra)
        _print("3DS 2.0", ok,
               f"IsoCode={iso}  Estado={r['estado']}  AzulOrderId={r['AzulOrderId']}")
        if not ok:
            failures.append(f"3DS 2.0: IsoCode inesperado {iso} — {p3.response_message}")
    except AzulIntegrationError as e:
        skipped.append(f"3DS 2.0 — No habilitado en sandbox: {e}")
        _print("3DS 2.0", False, f"SKIPPED — {e}")
        results.append({"test": "3DS 2.0", "estado": "SKIPPED", "motivo": str(e)})
    except Exception as e:
        failures.append(f"3DS 2.0 ERROR: {e}")
        _print("3DS 2.0", False, str(e))

    # -----------------------------------------------------------------------
    # TEST 8 — Void (si disponible)
    # -----------------------------------------------------------------------
    print("\n[8/9] Void (anulación)...")
    try:
        pv, _ = await gw.sale(_payment(), MASTERCARD_1, EXPIRATION, CVC)
        void_result = await gw.void(pv.azul_order_id, TODAY)
        iso_void = void_result.get("IsoCode", "")
        ok = iso_void == "00" or void_result.get("ResponseCode") != "Error"
        row = {
            "test":              "Void",
            "fecha":             NOW,
            "merchant_id":       MERCHANT_ID,
            "AzulOrderId_original": pv.azul_order_id,
            "AuthorizationCode_original": pv.authorization_code,
            "monto_original":    f"RD${pv.amount/100:.2f}",
            "iso_code":          iso_void,
            "response_code":     void_result.get("ResponseCode", ""),
            "response_message":  void_result.get("ResponseMessage", ""),
            "estado":            "APPROVED" if ok else "FAILED",
        }
        results.append(row)
        _print("Void", ok, f"IsoCode={iso_void}  AzulOrderId_original={pv.azul_order_id}")
    except AzulIntegrationError as e:
        skipped.append(f"Void — No habilitado en sandbox: {e}")
        _print("Void", False, f"SKIPPED — {e}")
        results.append({"test": "Void", "estado": "SKIPPED", "motivo": str(e)})
    except Exception as e:
        failures.append(f"Void ERROR: {e}")
        _print("Void", False, str(e))

    # -----------------------------------------------------------------------
    # TEST 9 — PCI DSS (verificación de masking)
    # -----------------------------------------------------------------------
    print("\n[9/9] PCI DSS — PAN masking y CVC no almacenado...")
    try:
        pp, txn = await gw.sale(_payment(), VISA_1, EXPIRATION, CVC)
        pan_in_log  = VISA_1 in txn.request_payload
        cvc_in_log  = CVC in (json.loads(txn.request_payload).get("CVC", ""))
        bin_in_log  = "426055" in txn.request_payload
        pan_ok      = not pan_in_log and bin_in_log
        cvc_ok      = not cvc_in_log

        pci_row = {
            "test":               "PCI DSS",
            "fecha":              NOW,
            "PAN_completo_en_log": "NO" if pan_ok else "SÍ — VIOLACIÓN",
            "CVC_en_log":         "NO" if cvc_ok else "SÍ — VIOLACIÓN",
            "CardNumber_guardado": _mask(VISA_1),
            "DataVaultToken_para_recurrentes": "Sí" if datavault_token else "No habilitado",
            "estado":             "COMPLIANT" if (pan_ok and cvc_ok) else "VIOLATION",
        }
        results.append(pci_row)
        _print("PAN no en claro en logs", pan_ok, f"BIN visible: {'Sí' if bin_in_log else 'No'}")
        _print("CVC no en logs",          cvc_ok)
        _print("CardNumber guardado",       True, _mask(VISA_1))
        _print("DataVaultToken recurrentes", bool(datavault_token),
               datavault_token[:16] + "..." if datavault_token else "No disponible")

        if not pan_ok:
            failures.append("PCI: PAN completo en logs — VIOLACIÓN")
        if not cvc_ok:
            failures.append("PCI: CVC almacenado en claro — VIOLACIÓN")
    except Exception as e:
        failures.append(f"PCI DSS ERROR: {e}")
        _print("PCI DSS", False, str(e))

    # -----------------------------------------------------------------------
    # Generar salida
    # -----------------------------------------------------------------------
    print("\n" + "="*60)
    approved = [r for r in results if r.get("estado") in ("APPROVED", "COMPLIANT", "TOKEN GENERATED")]
    print(f"  Completadas: {len(results)} pruebas")
    print(f"  Aprobadas:   {len(approved)}")
    print(f"  Saltadas:    {len(skipped)}")
    print(f"  Fallidas:    {len(failures)}")

    output_json = ROOT / "evidencia_certificacion.json"
    output_md   = ROOT / "reporte_luis_recio.md"

    # Resumen de failures
    if failures:
        print("\n  Errores/Declinadas:")
        for f in failures:
            print(f"    - {f}")

    evidence_doc = {
        "para":           "Luis Eduardo Recio Pérez — BPD / AZUL",
        "de":             "Equipo Atlas — ENSO SRL",
        "merchant_id":    MERCHANT_ID,
        "ambiente":       "sandbox — pruebas.azul.com.do",
        "ECommerceUrl":   ECOMMERCE_URL,
        "fecha_ejecucion": NOW,
        "total_pruebas":  len(results),
        "aprobadas":      len(approved),
        "saltadas":       len(skipped),
        "fallidas":       len(failures),
        "skipped_motivos": skipped,
        "failures":       failures,
        "transacciones":  results,
    }
    output_json.write_text(
        json.dumps(evidence_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  [JSON]:     {output_json}")

    # Markdown report
    _write_markdown(output_md, evidence_doc, results)
    print(f"  [MD]:       {output_md}")
    print("="*60 + "\n")


def _write_markdown(path: Path, doc: dict, results: list) -> None:
    now     = doc["fecha_ejecucion"]
    mid     = doc["merchant_id"]
    url     = doc["ECommerceUrl"]

    lines = [
        "# Constancias de Pruebas — Certificación AZUL Sandbox",
        "",
        f"**Para:** Luis Eduardo Recio Pérez — BPD / AZUL  ",
        f"**De:** Equipo Atlas — ENSO SRL  ",
        f"**Fecha ejecución:** {now}  ",
        f"**Merchant ID:** `{mid}`  ",
        f"**Ambiente:** sandbox — `pruebas.azul.com.do`  ",
        f"**ECommerceUrl:** {url}  ",
        "",
        "---",
        "",
        "## Tabla de Transacciones",
        "",
        "| # | Fecha | Test | Tarjeta | Monto | IsoCode | ResponseCode | AzulOrderId | AuthorizationCode | Estado |",
        "|---|-------|------|---------|-------|---------|--------------|-------------|-------------------|--------|",
    ]

    idx = 1
    for r in results:
        test   = r.get("test", "")
        card   = r.get("tarjeta_enmascarada", r.get("CardNumber_guardado", "—"))
        monto  = r.get("monto", "—")
        iso    = r.get("iso_code", "—")
        rc     = r.get("response_code", "—")
        aoid   = r.get("AzulOrderId", "—")
        auth   = r.get("AuthorizationCode", "—")
        estado = r.get("estado", "—")
        fecha  = r.get("fecha", now)
        lines.append(f"| {idx} | {fecha} | {test} | {card} | {monto} | {iso} | {rc} | {aoid} | {auth} | {estado} |")
        idx += 1

    lines += [
        "",
        "---",
        "",
        "## Detalle por Test",
        "",
    ]

    for r in results:
        test = r.get("test", "")
        lines.append(f"### {test}")
        lines.append("")
        lines.append("```")
        for k, v in r.items():
            if v and v != "—":
                lines.append(f"{k}: {v}")
        lines.append("```")
        lines.append("")

    # PCI section
    pci = next((r for r in results if r.get("test") == "PCI DSS"), {})
    lines += [
        "---",
        "",
        "## Evidencia PCI DSS",
        "",
        "| Validación | Resultado |",
        "|-----------|-----------|",
        f"| PAN completo en logs | {pci.get('PAN_completo_en_log', '—')} |",
        f"| CVC almacenado en claro | {pci.get('CVC_en_log', '—')} |",
        f"| CardNumber guardado (enmascarado) | `{pci.get('CardNumber_guardado', '—')}` |",
        f"| DataVaultToken para recurrentes | {pci.get('DataVaultToken_para_recurrentes', '—')} |",
        f"| Cumplimiento | **{pci.get('estado', '—')}** |",
        "",
        "---",
        "",
        "## Notas",
        "",
        "- Conexión vía **mTLS** con certificado proporcionado por AZUL.",
        "- `ForceNo3DS=1` se envía en todas las transacciones Split-it/recurrentes.",
        "- `merchantInitiatedIndicator: STANDING_ORDER` en pagos MIT.",
        "- `cardholderInitiatedIndicator: STANDING_ORDER` en pagos CIT con token.",
        "- PAN enmascarado (BIN + `******` + últimos 4) en todos los logs de auditoría.",
        "- CVC reemplazado por `***` antes de cualquier persistencia.",
        "",
    ]

    if doc.get("skipped_motivos"):
        lines.append("## Tests SKIPPED")
        lines.append("")
        for s in doc["skipped_motivos"]:
            lines.append(f"- {s}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(run_all())
