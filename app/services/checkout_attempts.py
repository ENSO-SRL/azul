"""Checkout and automatic billing share the same durable cycle reservation."""
from datetime import datetime, timezone
from app.services.billing_attempts import BillingAttempts
from app.services.subscription_identity import resolve_customer_identity, find_active_subscription
from app.services.scheduler import build_custom_order_id


async def reserve_checkout_charge(db, customer_id, payment_id=''):
    identity = await resolve_customer_identity(db, customer_id)
    sub = await find_active_subscription(db, identity)
    if sub is not None:
        cycle = (sub.next_charge_at or datetime.now(timezone.utc)).strftime('%Y%m%d')
        key = build_custom_order_id(sub.id, sub.failed_attempts, cycle)
        subscription_id = sub.id
    else:
        from sqlalchemy import select
        from app.infrastructure.models import RecurringPaymentModel
        prior = (await db.execute(select(RecurringPaymentModel.id).where(
            identity.matches(RecurringPaymentModel.customer_id)).order_by(
            RecurringPaymentModel.created_at.desc()).limit(1))).scalar_one_or_none()
        key = f'create:{identity.customer_id}:{prior or "initial"}'
        subscription_id = identity.customer_id
    await BillingAttempts(db).reserve(key, subscription_id, payment_id)
    return key
