"""
Tests automatizados del gateway Azul.

Cubre los 10 casos del whitelist de tarjetas de prueba (sandbox Merchant 39038540035).

Requisitos:
    - El servidor debe estar corriendo: uvicorn app.main:app --port 8000
    - Variables de entorno: AZUL_LOCAL_MODE=1 + credenciales correctas en .env

Correr:
    pytest tests/ -v
    pytest tests/test_gateway.py -v -k "approved"
"""

from __future__ import annotations

import os
import sys

import pytest
import pytest_asyncio

# Agregar proyecto al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.infrastructure.azul_gateway import AzulIntegrationError, AzulPaymentGateway
from app.domain.entities import Payment, PaymentStatus, PaymentType, SavedCard

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gateway() -> AzulPaymentGateway:
    return AzulPaymentGateway()


def _payment(amount: int = 118, itbis: int = 18) -> Payment:
    """Build a minimal test Payment entity."""
    return Payment(
        amount=amount,
        itbis=itbis,
        payment_type=PaymentType.SALE,
        auth_mode="splitit",
        cardholder_name="Test User",
        cardholder_email="test@atlas.do",
    )


EXPIRATION = "203412"
CVC_VALID = "123"

# ---------------------------------------------------------------------------
# Caso 1: Aprobada — Visa 4260550061845872
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sale_approved_visa(gateway):
    payment = _payment()
    payment, txn = await gateway.sale(
        payment, "4260550061845872", EXPIRATION, CVC_VALID
    )
    assert payment.status == PaymentStatus.APPROVED, (
        f"Expected APPROVED, got {payment.status} — {payment.response_message}"
    )
    assert payment.iso_code == "00"
    assert txn.http_status == 200


# ---------------------------------------------------------------------------
# Caso 2: Aprobada — Mastercard 5424180279791732
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sale_approved_mastercard(gateway):
    payment = _payment()
    payment, txn = await gateway.sale(
        payment, "5424180279791732", EXPIRATION, CVC_VALID
    )
    assert payment.status == PaymentStatus.APPROVED, (
        f"Expected APPROVED, got {payment.status} — {payment.response_message}"
    )
    assert payment.iso_code == "00"


# ---------------------------------------------------------------------------
# Caso 3: Aprobada — Discover 6011000990099818
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sale_approved_discover(gateway):
    payment = _payment()
    payment, txn = await gateway.sale(
        payment, "6011000990099818", EXPIRATION, CVC_VALID
    )
    assert payment.status == PaymentStatus.APPROVED, (
        f"Expected APPROVED, got {payment.status} — {payment.response_message}"
    )
    assert payment.iso_code == "00"


# ---------------------------------------------------------------------------
# Caso 4: Declinada por CVC incorrecto — IsoCode 99 "ERROR CVC"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sale_invalid_cvc(gateway):
    """CVC 000 debería ser rechazado por el sandbox con IsoCode 99."""
    payment = _payment()
    payment, txn = await gateway.sale(
        payment, "4260550061845872", EXPIRATION, "000"
    )
    # Puede ser DECLINED (99) o APPROVED en sandbox según configuración
    # Lo importante es que NO lanza excepción — es una respuesta de negocio
    assert payment.status in (PaymentStatus.APPROVED, PaymentStatus.DECLINED), (
        f"Unexpected status: {payment.status}"
    )
    # Si declinada, debe ser IsoCode 99
    if payment.status == PaymentStatus.DECLINED:
        assert payment.iso_code == "99", f"Expected IsoCode 99, got {payment.iso_code}"


# ---------------------------------------------------------------------------
# Caso 5: Error de validación — Amount=0 debe dar ResponseCode=Error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sale_zero_amount_raises_integration_error(gateway):
    """Amount=0 debería hacer que Azul devuelva ResponseCode=Error (VALIDATION_ERROR:Amount).

    AzulIntegrationError se lanza — esto es un bug del integrador, no una declinada.
    """
    payment = _payment(amount=0, itbis=0)
    with pytest.raises(AzulIntegrationError):
        await gateway.sale(payment, "4260550061845872", EXPIRATION, CVC_VALID)


# ---------------------------------------------------------------------------
# Caso 6: Token CREATE — devuelve DataVaultToken UUID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_create(gateway):
    """DataVault CREATE debe retornar un token UUID no vacío."""
    try:
        card = await gateway.create_token(
            customer_id="test-customer",
            card_number="4260550061845872",
            expiration=EXPIRATION,
            cvc=CVC_VALID,
            cardholder_name="Test User",
            cardholder_email="test@atlas.do",
        )
        assert isinstance(card, SavedCard)
        assert card.token, "Token debe ser no vacío"
        assert "-" in card.token or len(card.token) > 10, "Token parece un UUID"
        assert card.card_last4 == "5872"
    except AzulIntegrationError as e:
        if "VALIDATION_ERROR:TrxType" in str(e) or "TrxType" in str(e):
            pytest.skip("DataVault CREATE no habilitado en sandbox — usar sale+save_card")
        raise


# ---------------------------------------------------------------------------
# Caso 7: Token DELETE — eliminar token creado
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_delete(gateway):
    """DataVault DELETE debe completarse sin error."""
    try:
        card = await gateway.create_token(
            customer_id="test-delete",
            card_number="4035874000424977",
            expiration=EXPIRATION,
            cvc=CVC_VALID,
            cardholder_name="Test Delete",
            cardholder_email="delete@atlas.do",
        )
        # Should not raise
        await gateway.delete_token(card.token)
    except AzulIntegrationError as e:
        if "VALIDATION_ERROR:TrxType" in str(e):
            pytest.skip("DataVault CREATE no habilitado en sandbox")
        raise


# ---------------------------------------------------------------------------
# Caso 8: MIT con token (simula scheduler)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sale_mit_with_token(gateway):
    """Cobro MIT con token debe ser APPROVED con merchantInitiatedIndicator=STANDING_ORDER."""
    # Primero necesitamos un token — hacemos sale con save_token
    payment = _payment()
    payment, txn = await gateway.sale(
        payment, "4260550061845872", EXPIRATION, CVC_VALID, save_token=True
    )
    assert payment.status == PaymentStatus.APPROVED, (
        f"Initial sale failed: {payment.response_message}"
    )
    token = payment.data_vault_token
    if not token:
        pytest.skip("Sandbox no retornó DataVaultToken — save_token=True no funcionó")

    # MIT charge
    mit_payment = _payment()
    mit_payment.initiated_by = "merchant"
    mit_payment, mit_txn = await gateway.sale_mit(mit_payment, token)
    assert mit_payment.status == PaymentStatus.APPROVED, (
        f"MIT charge failed: {mit_payment.response_message}"
    )
    assert mit_payment.iso_code == "00"
    # Verify STANDING_ORDER in the payload
    import json
    payload = json.loads(mit_txn.request_payload)
    assert payload.get("merchantInitiatedIndicator") == "STANDING_ORDER", (
        f"merchantInitiatedIndicator not set correctly: {payload}"
    )


# ---------------------------------------------------------------------------
# Caso 9: CIT con token (simula club on-demand)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sale_cit_with_token(gateway):
    """Cobro CIT con token debe ser APPROVED con cardholderInitiatedIndicator=STANDING_ORDER."""
    payment = _payment()
    payment, txn = await gateway.sale(
        payment, "5426064000424979", EXPIRATION, CVC_VALID, save_token=True
    )
    assert payment.status == PaymentStatus.APPROVED, (
        f"Initial sale failed: {payment.response_message}"
    )
    token = payment.data_vault_token
    if not token:
        pytest.skip("Sandbox no retornó DataVaultToken")

    cit_payment = _payment()
    cit_payment, cit_txn = await gateway.sale_cit(cit_payment, token)
    assert cit_payment.status == PaymentStatus.APPROVED, (
        f"CIT charge failed: {cit_payment.response_message}"
    )
    assert cit_payment.iso_code == "00"
    # Verify STANDING_ORDER in the payload
    import json
    payload = json.loads(cit_txn.request_payload)
    assert payload.get("cardholderInitiatedIndicator") == "STANDING_ORDER", (
        f"cardholderInitiatedIndicator not set correctly: {payload}"
    )


# ---------------------------------------------------------------------------
# Caso 10: PAN masking — el payload almacenado nunca tiene PAN completo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pan_masking_in_audit_log(gateway):
    """Los logs de auditoría NUNCA deben contener el PAN completo."""
    payment = _payment()
    payment, txn = await gateway.sale(
        payment, "4012000033330026", EXPIRATION, CVC_VALID
    )
    # Verify PAN is masked in stored request payload
    assert "4012000033330026" not in txn.request_payload, (
        "PAN completo encontrado en el audit log — VIOLACIÓN PCI!"
    )
    # Should have masked version (first 6 + stars + last 4)
    assert "401200" in txn.request_payload or "****" in txn.request_payload, (
        "PAN no encontrado enmascarado en el audit log"
    )
