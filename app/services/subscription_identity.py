"""Resolve Atlas identities before reading or creating subscriptions.

public.users and explicitly reviewed historical IDs define account ownership.
The email supplied by a cardholder must never establish account ownership.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import SubscriptionStatus
from app.infrastructure.models import RecurringPaymentModel


class CustomerIdentityError(ValueError):
    """The account is missing, ambiguous, or already has duplicate subscriptions."""


@dataclass(frozen=True)
class CustomerIdentity:
    customer_id: str
    email: str
    aliases: tuple[str, ...]

    def matches(self, column):
        return func.lower(func.trim(column)).in_(self.aliases)


async def resolve_customer_identity(
    db: AsyncSession, customer_id: str,
) -> CustomerIdentity:
    identifier = str(customer_id).strip().lower()
    if not identifier:
        raise CustomerIdentityError("Falta el identificador del usuario de Atlas.")

    result = await db.execute(
        text(
            "SELECT id::text, lower(trim(email)), uuid::text FROM public.users "
            "WHERE id::text = :identifier OR uuid::text = :identifier "
            "OR lower(trim(email)) = :identifier "
            "OR id IN (SELECT atlas_user_id FROM pagos.customer_identity_aliases "
            "WHERE alias = :identifier) LIMIT 2"
        ),
        {"identifier": identifier},
    )
    rows = result.all()
    if len(rows) != 1:
        raise CustomerIdentityError(
            "No se pudo identificar una cuenta única de Atlas. "
            "Completa el registro del usuario antes de crear la suscripción."
        )
    canonical_id, email, user_uuid = rows[0]
    historical = (await db.execute(
        text("SELECT alias FROM pagos.customer_identity_aliases WHERE atlas_user_id = :user_id"),
        {"user_id": int(canonical_id)},
    )).all()
    aliases = tuple(sorted({
        str(value).strip().lower()
        for value in (canonical_id, email, user_uuid, identifier, *(row[0] for row in historical)) if value
    }))
    # A historical ID must never steal a different live account's identity.
    # Check all aliases even when the caller supplied the canonical ID.
    collisions = await db.execute(
        text(
            "SELECT id FROM public.users WHERE id::text <> :canonical "
            "AND (id::text IN :aliases OR uuid::text IN :aliases "
            "OR lower(trim(email)) IN :aliases) LIMIT 1"
        ).bindparams(bindparam("aliases", expanding=True)),
        {"canonical": str(canonical_id), "aliases": aliases},
    )
    if collisions.first() is not None:
        raise CustomerIdentityError("La identidad histórica entra en conflicto con otra cuenta de Atlas.")
    return CustomerIdentity(str(canonical_id), email or "", aliases)


async def lock_customer_subscriptions(db: AsyncSession, identity: CustomerIdentity) -> None:
    """Serialize writers for this person across workers until commit/rollback."""
    digest = hashlib.sha256(f"atlas-subscription:{identity.customer_id}".encode()).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


async def find_active_subscription(
    db: AsyncSession, identity: CustomerIdentity, *, for_update: bool = False,
) -> RecurringPaymentModel | None:
    query = select(RecurringPaymentModel).where(
        identity.matches(RecurringPaymentModel.customer_id),
        RecurringPaymentModel.status == SubscriptionStatus.ACTIVE.value,
    ).limit(2).execution_options(populate_existing=True)
    if for_update:
        query = query.with_for_update()
    rows = (await db.execute(query)).scalars().all()
    if len(rows) > 1:
        raise CustomerIdentityError(
            "La cuenta ya tiene varias suscripciones activas. "
            "Se requiere revisar los registros existentes antes de continuar."
        )
    return rows[0] if rows else None
