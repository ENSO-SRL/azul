"""Persist paid invoices and independently retry customer/accounting emails.

The outbox and invoice commit with the approved payment. The user service also
deduplicates the stable delivery ID before contacting the email provider.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, select

from app.infrastructure.models import InvoiceModel, InvoiceEmailModel
from app.services.invoice_document import ISSUER, LOGO, render_html, render_pdf, render_text

logger = logging.getLogger(__name__)
ACCEPTED = {'ACCEPTED', 'DELIVERED'}
MAX_ATTEMPTS = 8


def scope() -> str:
    return os.getenv('INVOICE_DELIVERY_SCOPE') or os.getenv('ATLAS_API_BASE_URL', '').rstrip('/')


async def record_invoice(db, payment, context: dict) -> InvoiceModel | None:
    """Caller holds the payment row lock. No I/O to a payment/mail provider."""
    if (payment.status.value != 'APPROVED' or payment.amount <= 0
            or context.get('kind') in ('refund', 'void', 'hold', 'hold_verify') or context.get('verification_only')
            or payment.order_id.startswith('HOLD-')
            or getattr(payment.payment_type, 'value', payment.payment_type) in ('REFUND', 'VOID')):
        return None
    # This issuer bills Atlas subscriptions/checkout charges, not third-party
    # utilities or club bookings collected on behalf of another establishment.
    if not (context.get('membership') or context.get('kind') == 'subscription'
            or payment.order_id.startswith('CHK-')):
        return None
    existing = (await db.execute(select(InvoiceModel).where(InvoiceModel.payment_id == payment.id))).scalar_one_or_none()
    if existing:
        return existing
    currency = getattr(payment.currency_code,'value',payment.currency_code)
    if currency not in ('DOP','USD'):
        currency = 'USD' if payment.currency in ('US$', 'USD') else 'DOP'
    paid_at = payment.created_at
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    local = paid_at.astimezone(ZoneInfo('America/Santo_Domingo'))
    tax = payment.itbis
    if not 0 <= tax <= payment.amount:
        raise ValueError('Invalid invoice tax/amount')
    recipient = (payment.cardholder_email or '').strip().lower()
    invoice = InvoiceModel(payment_id=payment.id, customer_id=payment.customer_id or '',
        currency=currency, subtotal=payment.amount-tax, itbis=tax, total=payment.amount,
        balance_due=0, issued_at=paid_at, document_json='{}', html_content='', pdf_content=b'', pdf_sha256='')
    db.add(invoice)
    await db.flush()
    number = f'INV-ATLAS-{local.year}-{invoice.id:06d}'
    month_names = ('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre')
    document = {'number':number, 'payment_id':payment.id, 'issuer':dict(ISSUER),
        'date':local.strftime('%d/%m/%Y'), 'paid_at':paid_at.isoformat(),
        'customer_name':payment.cardholder_name or 'Cliente iamAtlas', 'customer_email':recipient,
        'description':context.get('description') or f'Suscripción iamAtlas - {month_names[local.month-1]} {local.year}',
        'currency':currency, 'subtotal':invoice.subtotal, 'itbis':tax, 'total':payment.amount, 'balance_due':0,
        'tax_label':' (18%)' if tax and abs(tax*100-(payment.amount-tax)*18) <= 100 else '',
        'payment_method':'Tarjeta terminada en '+(payment.card_number_masked[-4:] or '****')}
    invoice.invoice_number = number
    invoice.document_json = json.dumps(document,ensure_ascii=False,sort_keys=True)
    accountant = os.getenv('ACCOUNTING_INVOICE_EMAIL','dmonegro@enso.do').strip().lower()
    destinations = [('customer',recipient), ('accounting',accountant)]
    seen = set()
    for audience,email in destinations:
        if not email or email in seen:
            continue
        seen.add(email)
        delivery=InvoiceEmailModel(id=f'invoice:{payment.id}:{audience}', invoice_id=invoice.id,
            scope=scope(), audience=audience, recipient=email, status='PENDING')
        db.add(delivery)
    return invoice


def prepare_document(invoice: InvoiceModel) -> None:
    """Caller holds the invoice lock, in a separate transaction from payment.

    A broken PDF renderer/asset must never turn an approved charge into an
    uncertain payment. Draft metadata/outbox are already durable at this point.
    """
    if invoice.pdf_content:
        return
    document=json.loads(invoice.document_json)
    pdf=render_pdf(document)
    html=render_html(document)
    document['email_text']=render_text(document)
    document['logo_base64']=base64.b64encode(LOGO.read_bytes()).decode()
    document['email_subjects']={audience:prefix+f'Factura {invoice.invoice_number} - iamAtlas'
        for audience,prefix in [('customer',''),('accounting','Copia contable - ')]}
    invoice.pdf_content=pdf
    invoice.pdf_sha256=hashlib.sha256(pdf).hexdigest()
    invoice.html_content=html
    invoice.document_json=json.dumps(document,ensure_ascii=False,sort_keys=True)


def email_payload(invoice: InvoiceModel, delivery: InvoiceEmailModel) -> dict:
    if delivery.payload_json and delivery.payload_json != '{}':
        return json.loads(delivery.payload_json)
    document = json.loads(invoice.document_json)
    if not invoice.pdf_content or 'email_text' not in document:
        raise ValueError('Invoice document is not ready')
    accounting = delivery.audience == 'accounting'
    intro = ('Copia contable para Daty Loritza Monegro Cuello.\n' if accounting else '')
    return {'notification_id':delivery.id, 'email':delivery.recipient,
        'subject':document['email_subjects'][delivery.audience],
        'html_content':invoice.html_content, 'text_content':intro+document['email_text'],
        'filename':invoice.invoice_number+'.pdf',
        'pdf_base64':base64.b64encode(invoice.pdf_content).decode(),
        'logo_base64':document['logo_base64']}


async def submit_delivery(payload: dict) -> dict:
    if os.getenv('PAYMENT_EMAIL_ENABLED','1') != '1':
        return {'status':'RETRYABLE','error':'EMAIL_DISABLED'}
    base, key = os.getenv('ATLAS_API_BASE_URL','').rstrip('/'), os.getenv('USER_SERVICE_KEY','')
    if not base or not key:
        return {'status':'RETRYABLE','error':'EMAIL_CONFIG_MISSING'}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(base+'/invoices/service/checkout_receipt', json=payload,
                headers={'x-service-key':key, 'Idempotency-Key':payload['notification_id']})
        if response.status_code == 200:
            result = response.json()
            if result.get('notification_id') == payload['notification_id'] and result.get('status') in (
                    'ACCEPTED','DELIVERED','RETRYABLE','SENDING','UNCERTAIN','FAILED'):
                return result
            return {'status':'UNCERTAIN','error':'INVALID_DELIVERY_ACK'}
        if response.status_code in (400,409,422):
            return {'status':'FAILED','error':f'USER_SERVICE_{response.status_code}'}
        return {'status':'RETRYABLE','error':f'USER_SERVICE_{response.status_code}'}
    except (httpx.HTTPError, ValueError):
        # Safe to retry this endpoint: its durable ID, not a POST to SendGrid,
        # determines whether a new provider call is permitted.
        return {'status':'RETRYABLE','error':'USER_SERVICE_UNAVAILABLE'}


async def dispatch_one(session_factory, delivery_id: str) -> str:
    now = datetime.now(timezone.utc)
    token = str(uuid.uuid4())
    async with session_factory() as db:
        job = (await db.execute(select(InvoiceEmailModel).where(InvoiceEmailModel.id==delivery_id,
            InvoiceEmailModel.scope==scope()).with_for_update(skip_locked=True))).scalar_one_or_none()
        if not job:
            return 'PENDING'
        if job.status in ACCEPTED | {'FAILED','UNCERTAIN'}:
            return job.status
        if (job.lease_until and job.lease_until>now) or job.next_attempt_at>now:
            return job.status
        if job.attempts >= MAX_ATTEMPTS:
            job.status='FAILED'; job.last_error='RETRY_LIMIT'; await db.commit()
            return job.status
        try:
            async with db.begin_nested():
                invoice=(await db.execute(select(InvoiceModel).where(InvoiceModel.id==job.invoice_id)
                    .with_for_update())).scalar_one()
                prepare_document(invoice)
                payload = email_payload(invoice,job)
                # Freeze the complete message before any provider attempt.
                job.payload_json=json.dumps(payload,sort_keys=True,ensure_ascii=False)
        except Exception:
            job.status='RETRYABLE'; job.attempts+=1; job.last_error='INVOICE_RENDER_FAILED'
            job.next_attempt_at=now+timedelta(seconds=min(3600,30*2**(job.attempts-1)))
            job.updated_at=now
            await db.commit()
            logger.exception('Invoice rendering deferred id=%s',delivery_id)
            return job.status
        job.status='SENDING'; job.attempts+=1; job.lease_token=token
        job.lease_until=now+timedelta(seconds=90); job.updated_at=now
        attempts=job.attempts
        await db.commit()
    result = await submit_delivery(payload)
    state = result['status']
    async with session_factory() as db:
        job = (await db.execute(select(InvoiceEmailModel).where(InvoiceEmailModel.id==delivery_id)
            .with_for_update())).scalar_one()
        if job.lease_token != token:
            return job.status
        job.status = 'RETRYABLE' if state == 'SENDING' else state
        job.provider_id = result.get('provider_id','')[:255]
        job.last_error = result.get('error','')[:100]
        job.lease_until = None
        job.next_attempt_at=now+timedelta(seconds=min(3600,30*2**(attempts-1)))
        job.updated_at=datetime.now(timezone.utc)
        await db.commit()
        return job.status


async def dispatch_invoice(payment_id: str, session_factory=None) -> str:
    if session_factory is None:
        from app.infrastructure.database import async_session
        session_factory=async_session
    async with session_factory() as db:
        jobs=list((await db.execute(select(InvoiceEmailModel).where(
            InvoiceEmailModel.id.like(f'invoice:{payment_id}:%'),InvoiceEmailModel.scope==scope()))).scalars())
        ids=[j.id for j in jobs]
        customer=next((j.id for j in jobs if j.audience=='customer'),None)
    states={}
    for delivery_id in ids:
        states[delivery_id]=await dispatch_one(session_factory,delivery_id)
    return states.get(customer,'NOT_REQUESTED')


async def customer_receipt_status(db, payment_id: str) -> str:
    job=await db.get(InvoiceEmailModel,f'invoice:{payment_id}:customer')
    return job.status if job and job.scope==scope() else 'NOT_REQUESTED'


async def retry_invoice_emails(session_factory) -> None:
    now=datetime.now(timezone.utc)
    async with session_factory() as db:
        ids=list((await db.execute(select(InvoiceEmailModel.id).where(InvoiceEmailModel.scope==scope(),
            InvoiceEmailModel.status.in_(['PENDING','RETRYABLE','SENDING']),
            InvoiceEmailModel.next_attempt_at<=now,
            or_(InvoiceEmailModel.lease_until.is_(None),InvoiceEmailModel.lease_until<now))
            .order_by(InvoiceEmailModel.next_attempt_at).limit(50))).scalars())
    for delivery_id in ids:
        try:
            await dispatch_one(session_factory,delivery_id)
        except Exception:
            logger.exception('Invoice email retry deferred id=%s',delivery_id)
