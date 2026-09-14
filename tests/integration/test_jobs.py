from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import timedelta
from sqlalchemy import select
from app.db.models import User, Entitlement, Order, now
from app.services.orders import OrderService
from app.jobs.worker import Jobs


async def test_failing_rows_do_not_starve_later_batches(db, seeded):
    async with db.transaction() as s:
        for i in range(205):
            user = User(telegram_user_id=1000 + i)
            s.add(user)
            await s.flush()
            s.add(
                Entitlement(
                    user_id=user.id, product_id=seeded.product.id, expires_at=now() - timedelta(days=1)
                )
            )
    remove = AsyncMock(side_effect=RuntimeError("API unavailable"))
    jobs = Jobs(SimpleNamespace(db=db, access=SimpleNamespace(remove=remove)))
    await jobs.expire_entitlements()
    assert remove.await_count == 205


async def test_reconciliation_expires_pending_orders(db, settings, seeded):
    order = await OrderService(db, settings).create(101, seeded.plans["fixed"].id, "old")
    async with db.transaction() as s:
        stored = await s.get(Order, order.id)
        stored.expires_at = now() - timedelta(hours=1)
    jobs = Jobs(SimpleNamespace(db=db, access=SimpleNamespace(remove=AsyncMock(), revoke=AsyncMock())))
    await jobs.reconciliation()
    async with db.sessions() as s:
        assert (await s.scalar(select(Order))).status == "expired"
