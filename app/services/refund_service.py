"""Serialized refunds with a durable balance and no implicit gateway retries."""
import json
import logging
from datetime import datetime,timezone
from sqlalchemy import select
from app.domain.entities import Payment,PaymentStatus,PaymentType,Currency
from app.infrastructure.models import PaymentModel,BillingAttemptModel,RecurringPaymentModel
from app.infrastructure.repo_impl import _model_to_payment
from app.services.billing_attempts import BillingConflict
from app.services.payment_lifecycle import begin_payment,complete_payment,mark_uncertain

async def refund_payment(db,original_id,amount,key,gateway):
    if db is None: raise BillingConflict('CONFLICT: La devolución requiere persistencia.')
    row=(await db.execute(select(PaymentModel).where(PaymentModel.id==original_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None: raise ValueError('Pago no encontrado.')
    original=_model_to_payment(row)
    from app.services.access_policy import utc
    original.created_at=utc(original.created_at)
    if original.status!=PaymentStatus.APPROVED:
        raise BillingConflict('CONFLICT: El pago no está aprobado o ya fue devuelto.')
    if not original.azul_order_id: raise ValueError('Falta referencia bancaria.')
    amount=original.amount if amount is None else amount
    if amount<=0 or amount>original.amount: raise ValueError('Importe de devolución inválido.')
    attempts=list((await db.execute(select(BillingAttemptModel).where(
        BillingAttemptModel.subscription_id=='refund:'+original_id))).scalars())
    if any(a.status in ('RESERVED','UNCERTAIN') for a in attempts):
        raise BillingConflict('CONFLICT: Hay una devolución pendiente de conciliación.')
    returned=sum(json.loads(a.context_json).get('refund_amount',0) for a in attempts if a.status=='APPROVED')
    if amount>original.amount-returned:
        raise BillingConflict('CONFLICT: La devolución supera el saldo disponible.')
    if amount!=original.amount and not key:
        raise BillingConflict('CONFLICT: Las devoluciones parciales requieren Idempotency-Key.')
    elapsed=(datetime.now(timezone.utc)-original.created_at).total_seconds()
    action='void' if elapsed<=1200 and amount==original.amount else 'refund'
    payment=Payment(amount=amount,itbis=min(original.itbis,amount),customer_id=original.customer_id,
        payment_type=PaymentType.VOID if action=='void' else PaymentType.REFUND,
        order_id='refund-'+original.id,cardholder_email=original.cardholder_email,
        currency=original.currency,currency_code=Currency.USD if original.currency=='US$' else Currency.DOP,
        idempotency_key=key or 'full-refund:'+original.id)
    payment,fresh=await begin_payment(db,payment,kind=action,key=payment.idempotency_key,
        context={'original_payment_id':original.id,'refund_amount':amount})
    if fresh:
        txn = None
        try:
            if action=='void':
                data=await gateway.void(azul_order_id=original.azul_order_id,original_date=original.created_at.strftime('%Y%m%d'))
                payment.iso_code=data.get('IsoCode','')
                payment.response_message=data.get('ResponseMessage','')
                payment.azul_order_id=data.get('AzulOrderId',original.azul_order_id)
                payment.status=PaymentStatus.APPROVED if payment.iso_code=='00' else PaymentStatus.DECLINED
            else:
                payment,txn=await gateway.refund(payment=payment,original_date=original.created_at.strftime('%Y%m%d'),
                    azul_order_id=original.azul_order_id,amount=amount)
        except Exception:
            await mark_uncertain(db,payment.id)
            raise
        await complete_payment(db,payment)
        if txn is not None:
            try:
                from app.infrastructure.repo_impl import SQLTransactionRepository
                await SQLTransactionRepository(db).save(txn)
            except Exception:
                await db.rollback()
                logging.getLogger(__name__).exception('Refund result persisted; transaction audit write failed')
    return {'payment_id':original_id,'action':action,
        'status':('CANCELLED' if action=='void' else 'REFUNDED') if payment.status==PaymentStatus.APPROVED else 'DECLINED',
        'iso_code':payment.iso_code,'response_message':payment.response_message,'azul_order_id':payment.azul_order_id}
