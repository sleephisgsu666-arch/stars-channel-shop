from __future__ import annotations

from uuid import UUID
from datetime import datetime
from collections.abc import Sequence
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta
from sqlalchemy import select
from app.db.models import Entitlement, Grant, now
from app.core.logging import event


def active(entitlement: Entitlement | None, at: datetime | None = None) -> bool:
    at = at or now()
    return bool(
        entitlement
        and entitlement.status == "active"
        and (entitlement.expires_at is None or entitlement.expires_at > at)
    )


def calculate_expiration(grants: Sequence[Grant]) -> tuple[bool, datetime | None]:
    """Replay non-refunded grants in original purchase order; None means lifetime."""
    expiration = None
    has_grant = False
    lifetime = False
    for grant in grants:
        if grant.revoked:
            continue
        has_grant = True
        if grant.kind == "lifetime" or (grant.kind in ("free", "manual") and grant.duration_days is None):
            lifetime = True
        elif not lifetime:
            if grant.kind == "recurring_30d":
                expiration = max(expiration, grant.period_end) if expiration else grant.period_end
            else:
                expiration = max(grant.created_at, expiration or grant.created_at) + timedelta(
                    days=grant.duration_days
                )
    return has_grant, None if lifetime else expiration


async def refresh(
    session: AsyncSession, entitlement: Entitlement, empty_status: str = "refunded"
) -> Entitlement:
    grants = list(
        await session.scalars(
            select(Grant).where(Grant.entitlement_id == entitlement.id).order_by(Grant.created_at, Grant.id)
        )
    )
    has_grant, expiration = calculate_expiration(grants)
    entitlement.expires_at = expiration
    entitlement.status = (
        ("active" if expiration is None or expiration > now() else "expired") if has_grant else empty_status
    )
    entitlement.removal_pending = entitlement.status != "active"
    return entitlement


async def grant_access(
    session: AsyncSession,
    user_id: UUID,
    product_id: UUID,
    plan_id: UUID | None,
    key: str,
    kind: str,
    duration: int | None = None,
    period_end: datetime | None = None,
    payment_id: UUID | None = None,
    order_id: UUID | None = None,
) -> Entitlement:
    # Caller owns the user/product advisory lock; row lock and UNIQUE are additional guards.
    ent = await session.scalar(
        select(Entitlement)
        .where(Entitlement.user_id == user_id, Entitlement.product_id == product_id)
        .with_for_update()
    )
    created = ent is None
    if ent is None:
        ent = Entitlement(user_id=user_id, product_id=product_id, plan_id=plan_id)
        session.add(ent)
        await session.flush()
    previous = await session.scalar(select(Grant).where(Grant.idempotency_key == key))
    if previous:
        return ent
    session.add(
        Grant(
            entitlement_id=ent.id,
            payment_id=payment_id,
            idempotency_key=key,
            kind=kind,
            duration_days=duration,
            period_end=period_end,
        )
    )
    ent.plan_id, ent.source_order_id = plan_id, order_id
    await session.flush()
    await refresh(session, ent)
    event("entitlement_created" if created else "entitlement_extended", entitlement_id=ent.id)
    return ent
