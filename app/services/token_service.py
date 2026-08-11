"""
Token service — manages DataVault card tokens for customers.

Provides register_card, delete_card, and list_cards use cases.
"""

from __future__ import annotations

from app.domain.entities import SavedCard
import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.repositories import SavedCardRepository
from app.infrastructure.azul_gateway import AzulIntegrationError, AzulPaymentGateway
from app.infrastructure.models import RecurringPaymentModel

logger = logging.getLogger(__name__)


class TokenService:

    def __init__(
        self,
        card_repo: SavedCardRepository,
        gateway: AzulPaymentGateway,
        db_session: AsyncSession | None = None,
    ):
        self._cards = card_repo
        self._gw    = gateway
        self._db    = db_session

    async def register_card(
        self,
        customer_id: str,
        card_number: str,
        expiration: str,
        cvc: str,
        cardholder_name: str = "",
        cardholder_email: str = "",
    ) -> SavedCard:
        """Store a card in Azul DataVault WITHOUT charging it.

        Uses TrxType=CREATE — the card is validated and tokenized.
        cardholder_name and cardholder_email are required by Azul API v1.2.
        Returns the SavedCard domain entity with the DataVault token.
        """
        card = await self._gw.create_token(
            customer_id=customer_id,
            card_number=card_number,
            expiration=expiration,
            cvc=cvc,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
        )
        
        # If this is the first card, make it the default
        existing_cards = await self._cards.list_by_customer(customer_id)
        if not existing_cards:
            card.is_default = True
            
        return await self._cards.save_if_not_exists(card)

    async def set_default_card(self, customer_id: str, card_id: str) -> None:
        """Set a specific card as the default for a customer."""
        card = await self._cards.get_by_id(card_id)
        if not card:
            raise ValueError(f"Card {card_id!r} not found.")
        if card.customer_id != customer_id:
            raise PermissionError(
                f"Card {card_id!r} does not belong to customer {customer_id!r}."
            )
        await self._cards.set_default(customer_id, card_id)

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
        try:
            await self._gw.delete_token(token)
        except AzulIntegrationError as e:
            logger.warning(f"Ignorando error al borrar token en Azul (DataVault): {e}")
        except Exception as e:
            logger.warning(f"Error de red o inesperado al borrar token en Azul: {e}")

        await self._cards.delete(token)

        # Cancel any ACTIVE recurring payments that used this token
        await self._cancel_subscriptions_for_token(token, customer_id)

    async def list_cards(self, customer_id: str) -> list[SavedCard]:
        """Return all saved cards for a customer, deduplicated by token.

        If duplicate tokens exist (legacy bug), only the first occurrence
        (most recent) is kept.
        """
        search_ids = {customer_id}
        if self._db:
            from sqlalchemy import text
            try:
                if customer_id.isdigit():
                    result = await self._db.execute(
                        text("SELECT email FROM public.users WHERE id = :cid LIMIT 1"),
                        {"cid": int(customer_id)},
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        search_ids.add(row[0])
                else:
                    result = await self._db.execute(
                        text("SELECT id FROM public.users WHERE email = :email LIMIT 1"),
                        {"email": customer_id},
                    )
                    row = result.fetchone()
                    if row and row[0]:
                        search_ids.add(str(row[0]))
            except Exception as e:
                logger.warning(f"Error fetching user cross-reference in list_cards: {e}")

        cards = []
        for sid in search_ids:
            cards.extend(await self._cards.list_by_customer(sid))
            
        # Deduplicate
        seen_tokens: set[str] = set()
        unique: list[SavedCard] = []
        # Sort cards by created_at desc to maintain the expected order
        cards.sort(key=lambda c: c.created_at, reverse=True)
        for c in cards:
            if c.token not in seen_tokens:
                seen_tokens.add(c.token)
                unique.append(c)
        return unique

    async def delete_card_by_id(self, card_id: str, customer_email: str) -> None:
        """Remove a card by its DB id. Verifies ownership by customer_id or email.

        The checkout passes the JWT `sub` (numeric ID like '173') as customer_email,
        while the card may have been saved with the same ID or the user's email.
        We accept both as valid ownership proof.

        Raises ValueError if card not found, PermissionError if ownership doesn't match.
        """
        card = await self._cards.get_by_id(card_id)
        if not card:
            raise ValueError(f"Tarjeta con id {card_id!r} no encontrada.")
        # Accept ownership if the caller's ID matches the card's customer_id
        # (could be numeric ID like "173" or email — both are valid)
        if card.customer_id != customer_email:
            # Also check if the caller's email matches (for cross-format lookups)
            # e.g., card saved with "173" but caller sends email, or vice versa
            if self._db:
                from sqlalchemy import text
                try:
                    if customer_email.isdigit():
                        result = await self._db.execute(
                            text("SELECT email FROM public.users WHERE id = :cid LIMIT 1"),
                            {"cid": int(customer_email)},
                        )
                        row = result.fetchone()
                        cross_id = row[0] if row else None
                    else:
                        result = await self._db.execute(
                            text("SELECT id FROM public.users WHERE email = :email LIMIT 1"),
                            {"email": customer_email},
                        )
                        row = result.fetchone()
                        cross_id = str(row[0]) if row else None
                        
                    if not (cross_id and card.customer_id == cross_id):
                        raise PermissionError(
                            f"La tarjeta no pertenece al usuario {customer_email!r}."
                        )
                except PermissionError:
                    raise
                except Exception:
                    # DB lookup failed — fall back to strict check
                    raise PermissionError(
                        f"La tarjeta no pertenece al usuario {customer_email!r}."
                    )
            else:
                raise PermissionError(
                    f"La tarjeta no pertenece al usuario {customer_email!r}."
                )

        try:
            await self._gw.delete_token(card.token)
        except AzulIntegrationError as e:
            logger.warning(f"Ignorando error al borrar token por ID en Azul (DataVault): {e}")
        except Exception as e:
            logger.warning(f"Error de red o inesperado al borrar token por ID en Azul: {e}")

        await self._cards.delete(card.token)

        # Cancel any ACTIVE recurring payments that used this token
        await self._cancel_subscriptions_for_token(card.token, card.customer_id)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _cancel_subscriptions_for_token(
        self, token: str, customer_id: str,
    ) -> None:
        """Cancel ACTIVE recurring payments tied to a deleted DataVault token."""
        if not self._db:
            logger.warning(
                "[token-svc] No DB session — skipping subscription cancellation "
                "for token=%s customer=%s",
                token[:12] + "…" if token else "(none)", customer_id,
            )
            return

        try:
            result = await self._db.execute(
                update(RecurringPaymentModel)
                .where(
                    RecurringPaymentModel.customer_id == customer_id,
                    RecurringPaymentModel.data_vault_token == token,
                    RecurringPaymentModel.status == "ACTIVE",
                )
                .values(status="CANCELLED")
            )
            await self._db.commit()

            rows_affected = getattr(result, "rowcount", 0) or 0
            if rows_affected > 0:
                logger.warning(
                    "[token-svc] ✓ cancelled %d subscription(s) for deleted token | "
                    "customer_id=%s token=%s",
                    rows_affected, customer_id, token[:12] + "…",
                )
        except Exception as exc:
            logger.error(
                "[token-svc] ✗ failed to cancel subscriptions | "
                "customer_id=%s token=%s err=%s",
                customer_id, token[:12] + "…" if token else "(none)", exc,
            )
