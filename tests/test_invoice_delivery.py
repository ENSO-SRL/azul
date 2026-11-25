"""Receipt state is independent of approval and cannot claim skipped mail."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.domain.entities import Payment, PaymentStatus
from app.services import invoice_delivery as delivery, post_payment
from app.services.invoice_document import ISSUER, render_html, render_pdf
from routers.checkout import _html_result


@pytest.mark.parametrize('state',['PENDING','RETRYABLE','SENDING','UNCERTAIN','FAILED','NOT_REQUESTED'])
def test_checkout_does_not_claim_unsent_invoice(state):
    html=_html_result('APPROVED','','fixture',59000,'00',cardholder_email='fixture@example.com',receipt_status=state)
    assert 'Recibo enviado' not in html and 'Factura aceptada para envío' not in html
    assert 'no lo repitas' in html and 'Pago aprobado' in html


def test_checkout_acceptance_requires_explicit_evidence():
    html=_html_result('APPROVED','','fixture',59000,'00',cardholder_email='fixture@example.com',receipt_status='ACCEPTED')
    assert 'Factura aceptada para envío' in html
    assert 'Factura aceptada' not in _html_result('APPROVED','','fixture',0,'',cardholder_email='fixture@example.com',receipt_status='ACCEPTED')


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['ACCEPTED','FAILED','UNCERTAIN','RETRYABLE'])
async def test_all_post_payment_results_preserve_payment_and_delivery_state(state):
    p=Payment(amount=59000,status=PaymentStatus.APPROVED,cardholder_email='fixture@example.com')
    with patch.object(delivery,'dispatch_invoice',AsyncMock(return_value=state)), \
         patch.object(post_payment,'_trigger_confirmation_email',AsyncMock(return_value=False)):
        result=await post_payment.handle_post_payment_actions(p)
    assert p.status==PaymentStatus.APPROVED
    assert result.email_sent is (state=='ACCEPTED')
    assert p._receipt_email_status==state


@pytest.mark.asyncio
async def test_missing_mail_config_is_retryable_without_http(monkeypatch):
    monkeypatch.delenv('USER_SERVICE_KEY',raising=False)
    with patch.object(httpx,'AsyncClient') as client:
        result=await delivery.submit_delivery({'notification_id':'fixture'})
    assert result=={'status':'RETRYABLE','error':'EMAIL_CONFIG_MISSING'}
    client.assert_not_called()


def fixture_document():
    return {'number':'INV-ATLAS-2026-000001','payment_id':'fixture-no-real-charge','issuer':ISSUER,
        'date':'25/11/2026','customer_name':'Cliente <prueba> & compañía','customer_email':'fixture@example.com',
        'description':'Suscripción iamAtlas - noviembre 2026','currency':'DOP','subtotal':50000,
        'itbis':9000,'total':59000,'balance_due':0,'tax_label':' (18%)','payment_method':'Tarjeta terminada en 4242'}


def test_invoice_html_and_pdf_show_actual_paid_total_not_bank_template():
    from io import BytesIO
    from pypdf import PdfReader
    d=fixture_document()
    html=render_html(d)
    assert 'Cliente &lt;prueba&gt; &amp; compañía' in html
    assert 'cid:invoice_logo' in html
    pdf=render_pdf(d)
    text='\n'.join(p.extract_text() for p in PdfReader(BytesIO(pdf)).pages)
    for value in ['590.00','500.00','90.00','0.00','133-11765-7','4242','PAGADA']:
        assert value in text
    assert '123456789' not in text and 'Transferencia bancaria' not in text
