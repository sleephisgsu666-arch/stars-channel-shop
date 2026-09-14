import asyncio
from datetime import timedelta
from uuid import uuid4
import pytest
from aiogram.types import SuccessfulPayment
from sqlalchemy import select, func
from app.core.security import Denied
from app.db.models import Entitlement, Payment, Plan, Order, now
from app.services.orders import OrderService
from app.services.payments import PaymentService


def success(order, charge=None, **changes):
    fields = dict(
        currency="XTR",
        total_amount=order.amount_stars,
        invoice_payload=str(order.id),
        telegram_payment_charge_id=charge or str(uuid4()),
        provider_payment_charge_id="",
    )
    if "subscription_expiration_date" in changes:
        changes["subscription_expiration_date"] = int(changes["subscription_expiration_date"].timestamp())
    fields.update(changes)
    return SuccessfulPayment(**fields)


async def make_order(db, settings, seeded, kind="fixed"):
    return await OrderService(db, settings).create(101, seeded.plans[kind].id, str(uuid4()))


async def test_precheckout_no_entitlement(db, settings, seeded):
    order = await make_order(db, settings, seeded)
    await PaymentService(db).precheckout(101, str(order.id), "XTR", 250)
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 0
        assert (await s.get(Order, order.id)).status == "precheckout_approved"


@pytest.mark.parametrize("uid,currency,amount", [(202, "XTR", 250), (101, "USD", 250), (101, "XTR", 1)])
async def test_payment_forgery(db, settings, seeded, uid, currency, amount):
    order = await make_order(db, settings, seeded)
    service = PaymentService(db)
    with pytest.raises(Denied):
        await service.precheckout(uid, str(order.id), currency, amount)
    with pytest.raises(Denied):
        await service.successful(uid, success(order, currency=currency, total_amount=amount))
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 0


async def test_payment_duplicate_concurrent(db, settings, seeded):
    order = await make_order(db, settings, seeded)
    payment = success(order)
    results = await asyncio.gather(*(PaymentService(db).successful(101, payment) for _ in range(4)))
    assert len({e.id for e in results}) == 1
    assert len({e.expires_at for e in results}) == 1
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 1
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 1


async def test_simultaneous_different_orders_extend(db, settings, seeded):
    orders = [await make_order(db, settings, seeded) for _ in range(2)]
    await asyncio.gather(*(PaymentService(db).successful(101, success(o)) for o in orders))
    async with db.sessions() as s:
        ent = await s.scalar(select(Entitlement))
        assert now() + timedelta(days=59) < ent.expires_at < now() + timedelta(days=61)


async def test_order_snapshots_price_and_duration(db, settings, seeded):
    order = await make_order(db, settings, seeded)
    async with db.transaction() as s:
        plan = await s.get(Plan, order.plan_id)
        plan.price_stars, plan.duration_days = 900, 90
    await PaymentService(db).precheckout(101, str(order.id), "XTR", 250)
    ent = await PaymentService(db).successful(101, success(order))
    assert now() + timedelta(days=29) < ent.expires_at < now() + timedelta(days=31)


async def test_free_once_even_after_expiration(db, settings, seeded):
    service = OrderService(db, settings)
    results = await asyncio.gather(*(service.free(101, seeded.plans["free"].id) for _ in range(3)))
    assert len({e.expires_at for e in results}) == 1
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 0
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 1


async def test_lifetime(db, settings, seeded):
    order = await make_order(db, settings, seeded, "lifetime")
    ent = await PaymentService(db).successful(101, success(order))
    assert ent.expires_at is None


async def test_recurring_renewal(db, settings, seeded):
    order = await make_order(db, settings, seeded, "recurring_30d")
    end = now().replace(microsecond=0) + timedelta(days=30)
    await PaymentService(db).successful(
        101, success(order, is_recurring=True, is_first_recurring=True, subscription_expiration_date=end)
    )
    ent = await PaymentService(db).successful(
        101, success(order, is_recurring=True, subscription_expiration_date=end + timedelta(days=30))
    )
    assert ent.expires_at == end + timedelta(days=30)


async def test_order_callback_idempotency(db, settings, seeded):
    service = OrderService(db, settings)
    a, b = await asyncio.gather(*(service.create(101, seeded.plans["fixed"].id, "same") for _ in range(2)))
    assert a.id == b.id
