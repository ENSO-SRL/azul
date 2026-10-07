"""Durable reservation before contacting the acquirer. Never retry an uncertain result."""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.infrastructure.models import BillingAttemptModel


class BillingConflict(ValueError):
    pass


class BillingAttempts:
    def __init__(self, db):
        self.db = db

    async def reserve(self, key, subscription_id, payment_id):
        if self.db is None:
            raise BillingConflict('CONFLICT: No se puede garantizar la exclusión del cobro.')
        self.db.add(BillingAttemptModel(id=key, subscription_id=subscription_id,
                     payment_id=payment_id, status='RESERVED'))
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise BillingConflict('CONFLICT: Este intento ya está reservado. Consulta su resultado antes de repetirlo.') from None

    async def finish(self, key, status, payment_id=''):
        row = (await self.db.execute(select(BillingAttemptModel).where(BillingAttemptModel.id == key).with_for_update())).scalar_one()
        row.status = status
        if payment_id:
            row.payment_id = payment_id
        row.updated_at = datetime.now(timezone.utc)
        await self.db.commit()

    async def uncertain(self, key):
        await self.db.rollback()
        await self.finish(key, 'UNCERTAIN')
