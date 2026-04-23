"""
SavedCard repository — SQLAlchemy async implementation.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import SavedCard
from app.domain.repositories import SavedCardRepository
from app.infrastructure.models import SavedCardModel


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------

def _card_to_model(c: SavedCard) -> SavedCardModel:
    return SavedCardModel(
        id=c.id,
        customer_id=c.customer_id,
        token=c.token,
        card_brand=c.card_brand,
        card_last4=c.card_last4,
        expiration=c.expiration,
        is_default=c.is_default,
        created_at=c.created_at,
    )


def _model_to_card(m: SavedCardModel) -> SavedCard:
    return SavedCard(
        id=m.id,
        customer_id=m.customer_id,
        token=m.token,
        card_brand=m.card_brand,
        card_last4=m.card_last4,
        expiration=m.expiration,
        is_default=m.is_default,
        created_at=m.created_at,
    )


# ---------------------------------------------------------------------------
# Concrete repo
# ---------------------------------------------------------------------------

class SQLSavedCardRepository(SavedCardRepository):

    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, card: SavedCard) -> SavedCard:
        self._session.add(_card_to_model(card))
        await self._session.commit()
        return card

    async def get_by_token(self, token: str) -> SavedCard | None:
        result = await self._session.execute(
            select(SavedCardModel).where(SavedCardModel.token == token)
        )
        row = result.scalar_one_or_none()
        return _model_to_card(row) if row else None

    async def list_by_customer(self, customer_id: str) -> list[SavedCard]:
        result = await self._session.execute(
            select(SavedCardModel)
            .where(SavedCardModel.customer_id == customer_id)
            .order_by(SavedCardModel.created_at.desc())
        )
        return [_model_to_card(r) for r in result.scalars().all()]

    async def delete(self, token: str) -> None:
        await self._session.execute(
            delete(SavedCardModel).where(SavedCardModel.token == token)
        )
        await self._session.commit()
