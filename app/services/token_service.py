"""
Token service — manages DataVault card tokens for customers.

Provides register_card, delete_card, and list_cards use cases.
"""

from __future__ import annotations

from app.domain.entities import SavedCard
from app.domain.repositories import SavedCardRepository
from app.infrastructure.azul_gateway import AzulPaymentGateway


class TokenService:

    def __init__(
        self,
        card_repo: SavedCardRepository,
        gateway: AzulPaymentGateway,
    ):
        self._cards = card_repo
        self._gw    = gateway

    async def register_card(
        self,
        customer_id: str,
        card_number: str,
        expiration: str,
        cvc: str,
    ) -> SavedCard:
        """Store a card in Azul DataVault WITHOUT charging it.

        Uses TrxType=CREATE — the card is validated and tokenized.
        Returns the SavedCard domain entity with the DataVault token.
        """
        card = await self._gw.create_token(
            customer_id=customer_id,
            card_number=card_number,
            expiration=expiration,
            cvc=cvc,
        )
        return await self._cards.save(card)

    async def delete_card(self, customer_id: str, token: str) -> None:
        """Remove a card from DataVault and from local DB.

        Verifies ownership — raises ValueError if token doesn't belong
        to customer_id (prevents cross-customer token deletion).
        """
        card = await self._cards.get_by_token(token)
        if not card:
            raise ValueError(f"Token {token!r} not found.")
        if card.customer_id != customer_id:
            raise PermissionError(
                f"Token {token!r} does not belong to customer {customer_id!r}."
            )

        # Delete from Azul DataVault first, then from local DB
        await self._gw.delete_token(token)
        await self._cards.delete(token)

    async def list_cards(self, customer_id: str) -> list[SavedCard]:
        """Return all saved cards for a customer."""
        return await self._cards.list_by_customer(customer_id)
