"""Persist payment intent before the external effect and recover local work afterwards.

Never infer a retry is safe from a timeout. A reserved/uncertain operation is
reconciled, not resubmitted. Only a definitive decline permits a new checkout.
No PAN, CVC or card token is stored in operation metadata.
"""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.domain.entities import PaymentStatus
from app.infrastructure.models import BillingAttemptModel, PaymentModel, SubscriptionActivationJobModel
from app.infrastructure.repo_impl import _payment_to_model, _model_to_payment
from app.services.billing_attempts import BillingConflict
from app.services.subscription_identity import resolve_customer_identity, lock_customer_subscriptions, find_active_subscription


async def subscription_history(db, identity):
    from app.infrastructure.models import RecurringPaymentModel
    return list((await db.execute(select(RecurringPaymentModel).where(
        identity.matches(RecurringPaymentModel.customer_id)))).scalars())


async def assert_no_pending_membership(db, identity):
    """Used by trial and charge writers while holding the same identity lock."""
    subs=await subscription_history(db,identity)
    owners=(*identity.aliases, *(s.id for s in subs))
    pending=(await db.execute(select(BillingAttemptModel.id).where(
        BillingAttemptModel.subscription_id.in_(owners),
        BillingAttemptModel.status.in_(['RESERVED','UNCERTAIN','PENDING_3DS']),
    ).limit(1))).first()
    jobs=(await db.execute(select(SubscriptionActivationJobModel.payment_id).where(
        SubscriptionActivationJobModel.customer_id.in_(identity.aliases),
        SubscriptionActivationJobModel.status=='PENDING').limit(1))).first()
    if pending or jobs:
        raise BillingConflict('CONFLICT: Hay un pago o activación pendiente. No repitas el cobro.')


async def begin_payment(db, payment, *, kind, key='', membership=False, context=None):
    if db is None:
        raise BillingConflict('CONFLICT: El cobro requiere persistencia e idempotencia.')
    context=dict(context or {})
    owner=('refund:'+context['original_payment_id']) if 'original_payment_id' in context else payment.customer_id
    if membership:
        identity=await resolve_customer_identity(db,owner)
        await lock_customer_subscriptions(db,identity)
        payment.customer_id=owner=identity.customer_id
        history=await subscription_history(db,identity)
        from app.services.access_policy import access_decision
        if access_decision(history)['allow_access']:
            raise BillingConflict('CONFLICT: Ya tienes un período vigente; no corresponde otro cobro.')
        await assert_no_pending_membership(db,identity)
        active=await find_active_subscription(db,identity)
        if active:
            from app.services.scheduler import build_custom_order_id
            cycle=(active.next_charge_at or datetime.now(timezone.utc)).strftime('%Y%m%d')
            key=build_custom_order_id(active.id,active.failed_attempts,cycle)
            owner=active.id
        else:
            prior=max(history,key=lambda r:r.created_at).id if history else 'initial'
            key=f'create:{identity.customer_id}:{prior}'
        # Rejected operations are immutable; reserve a new numbered attempt.
        base=key
        retry=0
        while (old:=await db.get(BillingAttemptModel,key)) is not None:
            if old.status!='DECLINED':
                raise BillingConflict('CONFLICT: Existe un intento anterior; consulta su resultado.')
            retry+=1
            key=f'{base}:retry{retry}'
    elif not key:
        raise BillingConflict('CONFLICT: Se requiere Idempotency-Key para este cobro.')
    else:
        key='op:'+hashlib.sha256(key.encode()).hexdigest()
    contract={name:getattr(payment,name) for name in
              ('customer_id','amount','itbis','currency','order_id','service_type','bill_reference')}
    contract.update(kind=kind,currency_code=payment.currency_code.value,context=context,membership=membership)
    fingerprint=hashlib.sha256(json.dumps(contract,sort_keys=True).encode()).hexdigest()
    context.update(kind=kind,membership=membership)
    existing=await db.get(BillingAttemptModel,key)
    if existing:
        return await _existing(db,existing,fingerprint)
    if not membership and payment.idempotency_key:
        legacy=(await db.execute(select(PaymentModel).where(
            PaymentModel.idempotency_key==payment.idempotency_key))).scalar_one_or_none()
        if legacy:
            if any(getattr(legacy,n)!=getattr(payment,n) for n in
                ('customer_id','amount','itbis','currency','order_id','service_type','bill_reference')):
                raise BillingConflict('CONFLICT: La clave pertenece a otro pago.')
            if legacy.status in ('APPROVED','DECLINED','VOIDED','REFUNDED'):
                return _model_to_payment(legacy),False
            raise BillingConflict('CONFLICT: El pago anterior requiere conciliación.')
    attempt=BillingAttemptModel(id=key,subscription_id=owner or 'api',payment_id=payment.id,
        request_fingerprint=fingerprint,context_json=json.dumps(context),status='RESERVED')
    db.add(attempt)
    db.add(_payment_to_model(payment))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing=await db.get(BillingAttemptModel,key)
        if existing is None: raise
        return await _existing(db,existing,fingerprint)
    return payment,True


async def _existing(db,attempt,fingerprint):
    if attempt.request_fingerprint!=fingerprint:
        raise BillingConflict('CONFLICT: La clave ya corresponde a otra operación o importe.')
    row=await db.get(PaymentModel,attempt.payment_id)
    if row is None or attempt.status in ('RESERVED','UNCERTAIN'):
        raise BillingConflict('CONFLICT: Resultado pendiente de conciliación; no repitas el pago.')
    return _model_to_payment(row),False


async def mark_uncertain(db,payment_id):
    await db.rollback()
    for row in (await db.execute(select(BillingAttemptModel).where(
        BillingAttemptModel.payment_id==payment_id).with_for_update())).scalars():
        if row.status in ('RESERVED','PENDING_3DS'):
            row.status='UNCERTAIN';row.updated_at=datetime.now(timezone.utc)
    await db.commit()


async def complete_payment(db,payment):
    """Commit result, ledger and activation together, before optional side effects."""
    if payment.status == PaymentStatus.DECLINED and not (
        len(payment.iso_code or '') == 2 and payment.iso_code.isdigit()
    ):
        await mark_uncertain(db, payment.id)
        raise BillingConflict('CONFLICT: Respuesta bancaria incompleta; se requiere conciliación.')
    row=(await db.execute(select(PaymentModel).where(PaymentModel.id==payment.id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    previous_status=row.status if row else None
    if previous_status in ('APPROVED','VOIDED','REFUNDED') and payment.status.value != previous_status:
        # A delayed 3DS HTTP response must never downgrade a committed result.
        payment.__dict__.update(_model_to_payment(row).__dict__)
        await db.commit()
        return
    if row is None:
        row=_payment_to_model(payment);db.add(row)
    else:
        model=_payment_to_model(payment)
        for column in PaymentModel.__table__.columns:
            if column.name!='id': setattr(row,column.name,getattr(model,column.name))
    attempts=list((await db.execute(select(BillingAttemptModel).where(
        BillingAttemptModel.payment_id==payment.id).with_for_update())).scalars())
    for attempt in attempts:
        attempt.status='PENDING_3DS' if payment.status.value.startswith('PENDING_3DS') else payment.status.value
        attempt.updated_at=datetime.now(timezone.utc)
        context=json.loads(attempt.context_json or '{}')
        if context.get('kind') in ('void','refund') and payment.status==PaymentStatus.APPROVED:
            from app.infrastructure.models import RecurringPaymentModel
            original=(await db.execute(select(PaymentModel).where(
                PaymentModel.id==context['original_payment_id']).with_for_update())).scalar_one()
            refunds=(await db.execute(select(BillingAttemptModel).where(
                BillingAttemptModel.subscription_id=='refund:'+original.id,
                BillingAttemptModel.status=='APPROVED'))).scalars()
            total=sum(json.loads(a.context_json).get('refund_amount',0) for a in refunds)
            if total>=original.amount:
                original.status='VOIDED' if context['kind']=='void' else 'REFUNDED'
                activation=(await db.execute(select(SubscriptionActivationJobModel).where(
                    SubscriptionActivationJobModel.payment_id==original.id).with_for_update())).scalar_one_or_none()
                if activation:
                    activation.status='CANCELLED'
                if original.customer_id:
                    identity=await resolve_customer_identity(db,original.customer_id)
                    subs=(await db.execute(select(RecurringPaymentModel).where(
                        identity.matches(RecurringPaymentModel.customer_id),
                        RecurringPaymentModel.last_charged_at==original.created_at).with_for_update())).scalars()
                    for sub in subs:
                        sub.last_charged_at=None;sub.status='PAUSED'
        if (context.get('membership') and context.get('kind') in ('sale','checkout_token','subscription')
                and payment.status==PaymentStatus.DECLINED and previous_status!='DECLINED'):
            from app.infrastructure.models import RecurringPaymentModel
            sub=await db.get(RecurringPaymentModel,attempt.subscription_id)
            if sub and sub.status=='ACTIVE':
                from app.services.scheduler import _handle_failure
                _handle_failure(sub,payment.response_message or payment.iso_code)
                payment._failure_applied = True
        if context.get('membership') and payment.status==PaymentStatus.APPROVED:
            job=await db.get(SubscriptionActivationJobModel,payment.id)
            if job is None:
                db.add(SubscriptionActivationJobModel(payment_id=payment.id,customer_id=payment.customer_id,
                    card_expiration=context.get('card_expiration',''),promo_code=context.get('promo_code','') or '',
                    user_name=context.get('user_name',''),status='PENDING'))
    await db.commit()


def serialized_3ds(phase):
    """Durable phase latch: repeated browser/callback requests never resend to bank."""
    from functools import wraps
    def decorate(fn):
        @wraps(fn)
        async def wrapped(self,payment_id,*args,**kwargs):
            payment=await self._payments.get_by_id(payment_id)
            if payment is None: raise ValueError('Pago no encontrado')
            expected='PENDING_3DS_METHOD' if phase=='method' else 'PENDING_3DS_CHALLENGE'
            if payment.status.value!=expected:
                if payment.status.value in ('APPROVED','DECLINED','VOIDED','REFUNDED','PENDING_3DS_CHALLENGE'):
                    return payment
                raise BillingConflict('CONFLICT: Estado 3DS pendiente de revisión.')
            from app.services.billing_attempts import BillingAttempts
            await BillingAttempts(self._db).reserve(f'3ds:{phase}:{payment_id}',payment.customer_id,payment_id)
            try:
                return await fn(self,payment_id,*args,**kwargs)
            except Exception:
                await mark_uncertain(self._db,payment_id)
                raise
        return wrapped
    return decorate
