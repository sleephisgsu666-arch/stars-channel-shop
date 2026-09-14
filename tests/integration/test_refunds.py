import asyncio
from datetime import timedelta
from sqlalchemy import select, func
from aiogram.types import SuccessfulPayment
from app.db.models import Payment, Entitlement, AuditLog, now
from app.services.orders import OrderService
from app.services.payments import PaymentService
from app.services.refunds import RefundService
from app.services.subscriptions import SubscriptionService


async def pay(db, settings, seeded, key="one"):
    order = await OrderService(db, settings).create(101, seeded.plans["fixed"].id, key)
    ent = await PaymentService(db).successful(
        101,
        SuccessfulPayment(
            currency="XTR",
            total_amount=250,
            invoice_payload=str(order.id),
            telegram_payment_charge_id=key,
            provider_payment_charge_id="",
        ),
    )
    async with db.sessions() as s:
        payment = await s.scalar(select(Payment).where(Payment.order_id == order.id))
    return payment, ent


async def test_refund_once_and_revoke(db, settings, seeded, bot):
    payment, ent = await pay(db, settings, seeded)
    service = RefundService(db, bot, settings)
    await service.request(100, payment.id, "refund-once")
    await service.request(100, payment.id, "refund-again")
    await asyncio.gather(service.process(payment.id), service.process(payment.id))
    bot.refund_star_payment.assert_awaited_once()
    async with db.sessions() as s:
        assert (await s.get(Payment, payment.id)).status == "refunded"
        access = await s.get(Entitlement, ent.id)
        assert access.status == "refunded" and access.removal_pending
        assert await s.scalar(select(func.count()).select_from(AuditLog)) == 2


async def test_refund_preserves_other_payment(db, settings, seeded, bot):
    payment, ent = await pay(db, settings, seeded)
    await pay(db, settings, seeded, "two")
    service = RefundService(db, bot, settings)
    await service.request(100, payment.id, "refund")
    await service.process(payment.id)
    async with db.sessions() as s:
        access = await s.get(Entitlement, ent.id)
        assert access.status == "active"
        assert now() + timedelta(days=29) < access.expires_at < now() + timedelta(days=31)


async def test_subscription_state_does_not_grant_access(db, settings, seeded, bot):
    order = await OrderService(db, settings).create(101, seeded.plans["recurring_30d"].id, "sub")
    await SubscriptionService(db, bot).updated(101, str(order.id), "active")
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 0


async def test_second_charge_same_invoice_refunded_without_losing_first_access(db, settings, seeded, bot):
    payment, ent = await pay(db, settings, seeded)
    async with db.sessions() as s:
        original_expiration = (await s.get(Entitlement, ent.id)).expires_at
    outcome = await PaymentService(db).successful(
        101,
        SuccessfulPayment(
            currency="XTR",
            total_amount=250,
            invoice_payload=str(payment.order_id),
            telegram_payment_charge_id="extra-charge",
            provider_payment_charge_id="",
        ),
    )
    assert outcome is None
    async with db.sessions() as s:
        extra = await s.scalar(select(Payment).where(Payment.telegram_payment_charge_id == "extra-charge"))
    await RefundService(db, bot, settings).process(extra.id)
    async with db.sessions() as s:
        access = await s.get(Entitlement, ent.id)
        assert access.status == "active" and access.expires_at == original_expiration
        assert (await s.get(Payment, payment.id)).status == "paid"


async def test_refund_uncertain_response_retries(db, settings, seeded, bot):
    from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
    from aiogram.methods import RefundStarPayment
    import pytest

    payment, ent = await pay(db, settings, seeded)
    service = RefundService(db, bot, settings)
    await service.request(100, payment.id, "retry-refund")
    method = RefundStarPayment(user_id=101, telegram_payment_charge_id=payment.telegram_payment_charge_id)
    bot.refund_star_payment.side_effect = [
        TelegramNetworkError(method=method, message="timeout"),
        TelegramBadRequest(method=method, message="CHARGE_ALREADY_REFUNDED"),
    ]
    with pytest.raises(TelegramNetworkError):
        await service.process(payment.id)
    await service.process(payment.id)
    async with db.sessions() as s:
        assert (await s.get(Payment, payment.id)).status == "refunded"


async def test_refund_before_payment_never_grants_access(db, settings, seeded, bot):
    from aiogram.types import RefundedPayment

    order = await OrderService(db, settings).create(101, seeded.plans["fixed"].id, "reordered")
    fields = dict(
        currency="XTR",
        total_amount=250,
        invoice_payload=str(order.id),
        telegram_payment_charge_id="reordered-charge",
        provider_payment_charge_id="",
    )
    await RefundService(db, bot, settings).external(101, RefundedPayment(**fields))
    outcome = await PaymentService(db).successful(101, SuccessfulPayment(**fields))
    assert outcome is None
    async with db.sessions() as s:
        assert (await s.scalar(select(Payment))).status == "refunded"
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 0


async def test_refund_and_payment_concurrent(db, settings, seeded, bot):
    from aiogram.types import RefundedPayment

    order = await OrderService(db, settings).create(101, seeded.plans["fixed"].id, "concurrent")
    fields = dict(
        currency="XTR",
        total_amount=250,
        invoice_payload=str(order.id),
        telegram_payment_charge_id="parallel-charge",
        provider_payment_charge_id="",
    )
    await asyncio.gather(
        RefundService(db, bot, settings).external(101, RefundedPayment(**fields)),
        PaymentService(db).successful(101, SuccessfulPayment(**fields)),
    )
    async with db.sessions() as s:
        assert (await s.scalar(select(Payment))).status == "refunded"
        ent = await s.scalar(select(Entitlement))
        assert ent is None or ent.status == "refunded"
