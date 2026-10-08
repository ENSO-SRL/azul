"""Resolve Atlas identities before reading or creating subscriptions.

public.users and explicitly reviewed historical IDs define account ownership.
The email supplied by a cardholder must never establish account ownership.
"""

from __future__ import annotations

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
            "SELECT DISTINCT canonical.id::text, lower(trim(canonical.email)), canonical.uuid::text "
            "FROM public.users candidate "
            "LEFT JOIN pagos.customer_identity_links link ON link.source_user_id = candidate.id "
            "JOIN public.users canonical ON canonical.id = coalesce(link.atlas_user_id, candidate.id) "
            "WHERE candidate.id::text = :identifier OR candidate.uuid::text = :identifier "
            "OR lower(trim(candidate.email)) = :identifier "
            "OR candidate.id IN (SELECT atlas_user_id FROM pagos.customer_identity_aliases "
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
    members = (await db.execute(
        text("SELECT id, lower(trim(email)), uuid::text FROM public.users "
             "WHERE id = :user_id OR id IN (SELECT source_user_id FROM pagos.customer_identity_links "
             "WHERE atlas_user_id = :user_id)"),
        {"user_id": int(canonical_id)},
    )).all()
    member_ids = tuple(row[0] for row in members)
    # The migration rejects chains. Also fail closed on malformed restored data.
    chain = (await db.execute(
        text("SELECT source_user_id FROM pagos.customer_identity_links "
             "WHERE source_user_id = :canonical OR "
             "(atlas_user_id IN :members AND atlas_user_id <> :canonical) LIMIT 1")
        .bindparams(bindparam("members", expanding=True)),
        {"canonical": int(canonical_id), "members": member_ids},
    )).first()
    if chain is not None:
        raise CustomerIdentityError("La vinculación de facturación requiere revisión: contiene una cadena.")
    historical = (await db.execute(
        text("SELECT alias FROM pagos.customer_identity_aliases WHERE atlas_user_id IN :members")
        .bindparams(bindparam("members", expanding=True)),
        {"members": member_ids},
    )).all()
    aliases = tuple(sorted({
        str(value).strip().lower()
        for value in (identifier, *(v for row in members for v in row), *(row[0] for row in historical)) if value
    }))
    # Only reviewed group members may share ownership. Check every key even
    # when the caller supplied the primary ID, including other historical owners.
    collisions = await db.execute(
        text(
            "SELECT id FROM public.users WHERE id NOT IN :members "
            "AND (id::text IN :aliases OR uuid::text IN :aliases "
            "OR lower(trim(email)) IN :aliases) LIMIT 1"
        ).bindparams(bindparam("aliases", expanding=True), bindparam("members", expanding=True)),
        {"members": member_ids, "aliases": aliases},
    )
    if collisions.first() is not None:
        raise CustomerIdentityError("La identidad histórica entra en conflicto con otra cuenta de Atlas.")
    historical_collision = (await db.execute(
        text("SELECT alias FROM pagos.customer_identity_aliases "
             "WHERE atlas_user_id NOT IN :members AND alias IN :aliases LIMIT 1")
        .bindparams(bindparam("members", expanding=True), bindparam("aliases", expanding=True)),
        {"members": member_ids, "aliases": aliases},
    )).first()
    if historical_collision is not None:
        raise CustomerIdentityError("La identidad de facturación coincide con un alias de otra cuenta.")
    return CustomerIdentity(str(canonical_id), email or "", aliases)


async def lock_customer_subscriptions(db: AsyncSession, identity: CustomerIdentity) -> None:
    """Serialize writers for this person across workers until commit/rollback."""
    # Use the very same PostgreSQL key as the trigger guarding direct SQL writers.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('atlas-subscription:' || :canonical, 0))"),
        {"canonical": identity.customer_id},
    )


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
