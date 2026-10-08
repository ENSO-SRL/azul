"""Recover a known operation by querying the bank; never resubmit its charge."""
import json
import logging
from datetime import datetime,timedelta,timezone
from sqlalchemy import select
from app.domain.entities import PaymentStatus
from app.infrastructure.models import BillingAttemptModel,PaymentModel
from app.infrastructure.repo_impl import _model_to_payment
from app.services.payment_lifecycle import complete_payment

logger=logging.getLogger(__name__)

async def recover_payment_operations(db,gateway):
    operations=list((await db.execute(select(BillingAttemptModel.id).where(
        BillingAttemptModel.request_fingerprint!='',
        ~BillingAttemptModel.subscription_id.like('refund:%'),
        BillingAttemptModel.updated_at < datetime.now(timezone.utc)-timedelta(minutes=5),
        BillingAttemptModel.status.in_(['RESERVED','UNCERTAIN','PENDING_3DS'])
    ).order_by(BillingAttemptModel.updated_at).limit(100))).scalars())
    for operation_id in operations:
        operation=await db.get(BillingAttemptModel,operation_id)
        if operation is None or operation.status not in ('RESERVED','UNCERTAIN','PENDING_3DS'):
            continue
        row=await db.get(PaymentModel,operation.payment_id)
        if row is None: continue
        context=json.loads(operation.context_json or '{}')
        if context.get('kind') in ('void','refund'): continue  # separate refund reconciliation
        # Rotate reviewed operations so unresolved rows cannot starve newer work.
        operation.updated_at=datetime.now(timezone.utc)
        payment_id=operation.payment_id
        await db.commit()
        try:
            data=await gateway.verify_payment(row.id)
            found=data.get('Found') in (True,1,'true','True')
            # Incomplete evidence remains blocked for review, never converted to a retry.
            amount=data.get('Amount')
            currency=data.get('CurrencyPosCode')
            if (not found or str(amount)!=str(row.amount) or currency!=row.currency
                    or data.get('CustomOrderId',row.id)!=row.id
                    or not (data.get('AzulOrderId') or data.get('AZULOrderId'))):
                logger.warning('Payment requires manual reconciliation payment_id=%s',row.id)
                continue
            if data.get('IsoCode')!='00':
                # A negative verify result needs provider-specific interpretation.
                continue
            payment=_model_to_payment(row)
            payment.status=PaymentStatus.APPROVED;payment.iso_code='00'
            payment.azul_order_id=data.get('AzulOrderId') or data['AZULOrderId']
            payment.authorization_code=data.get('AuthorizationCode','')
            payment.data_vault_token=data.get('DataVaultToken') or payment.data_vault_token
            await complete_payment(db,payment)
        except Exception:
            await db.rollback()
            logger.exception('Payment recovery deferred payment_id=%s',payment_id)
