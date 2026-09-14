import asyncio
from uuid import uuid4
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from aiogram.types import SuccessfulPayment, RefundedPayment
from app.services.wallet import WalletService
from app.services.payments import PaymentService
from app.services.refunds import RefundService
from app.db.models import User, Order, Payment, WalletEntry, Entitlement, now
from app.core.security import Denied


async def topup(db, settings, amount=500, key=None):
    service = WalletService(db, settings)
    order = await service.create_topup(101, amount, key or str(uuid4()))
    data = SuccessfulPayment(
        currency="XTR",
        total_amount=amount,
        invoice_payload=str(order.id),
        telegram_payment_charge_id=str(uuid4()),
        provider_payment_charge_id="",
    )
    await PaymentService(db).successful(101, data)
    async with db.sessions() as s:
        payment = await s.scalar(
            select(Payment).where(Payment.telegram_payment_charge_id == data.telegram_payment_charge_id)
        )
    return order, data, payment


async def assert_balanced(db, expected):
    async with db.sessions() as s:
        user = await s.scalar(select(User).where(User.telegram_user_id == 101))
        total = await s.scalar(
            select(func.coalesce(func.sum(WalletEntry.amount), 0)).where(WalletEntry.user_id == user.id)
        )
        assert user.balance_stars == expected == total


async def test_topup_never_grants_access_and_duplicate_charge_once(db, settings, seeded):
    order, data, _ = await topup(db, settings)
    await asyncio.gather(*(PaymentService(db).successful(101, data) for _ in range(4)))
    await assert_balanced(db, 500)
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 0
        assert await s.scalar(select(func.count()).select_from(Payment)) == 1


async def test_precheckout_does_not_credit(db, settings, seeded):
    order = await WalletService(db, settings).create_topup(101, 500, "topup")
    await PaymentService(db).precheckout(101, str(order.id), "XTR", 500)
    await assert_balanced(db, 0)


@pytest.mark.parametrize("uid,currency,amount", [(202, "XTR", 500), (101, "USD", 500), (101, "XTR", 1)])
async def test_forged_topup(db, settings, seeded, uid, currency, amount):
    order = await WalletService(db, settings).create_topup(101, 500, "topup")
    payment = SuccessfulPayment(
        currency=currency,
        total_amount=amount,
        invoice_payload=str(order.id),
        telegram_payment_charge_id="bad",
        provider_payment_charge_id="",
    )
    with pytest.raises(Denied):
        await PaymentService(db).successful(uid, payment)
    await assert_balanced(db, 0)


async def test_purchase_spends_once_under_race(db, settings, seeded):
    await topup(db, settings)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "buy")
    await asyncio.gather(*(service.purchase(101, order.id) for _ in range(4)))
    await assert_balanced(db, 250)
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 1
        ent = await s.scalar(select(Entitlement))
        assert now() + timedelta(days=29) < ent.expires_at < now() + timedelta(days=31)


async def test_two_different_purchases_cannot_overspend(db, settings, seeded):
    await topup(db, settings, 250)
    service = WalletService(db, settings)
    orders = [await service.quote(101, seeded.plans["fixed"].id, str(uuid4())) for _ in range(2)]
    results = await asyncio.gather(*(service.purchase(101, o.id) for o in orders), return_exceptions=True)
    assert sum(isinstance(r, Denied) for r in results) == 1
    await assert_balanced(db, 0)


async def test_insufficient_balance_and_idor(db, settings, seeded):
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "buy")
    with pytest.raises(Denied):
        await service.purchase(101, order.id)
    await topup(db, settings)
    with pytest.raises(Denied):
        await service.purchase(202, order.id)
    await assert_balanced(db, 500)


async def test_lifetime_and_no_recurring(db, settings, seeded):
    await topup(db, settings, 1500)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["lifetime"].id, "life")
    ent = await service.purchase(101, order.id)
    assert ent.expires_at is None
    with pytest.raises(Denied):
        await service.quote(101, seeded.plans["recurring_30d"].id, "sub")
    await assert_balanced(db, 0)


async def test_purchase_refund_returns_balance_once(db, settings, seeded):
    await topup(db, settings)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "buy")
    await service.purchase(101, order.id)
    await service.refund_purchase(100, order.id, "refund")
    await service.refund_purchase(100, order.id, "refund2")
    await assert_balanced(db, 500)
    async with db.sessions() as s:
        ent = await s.scalar(select(Entitlement))
        assert ent.status == "refunded" and ent.removal_pending


async def test_topup_refund_reserves_balance_until_telegram_confirmation(db, settings, seeded, bot):
    _, _, payment = await topup(db, settings)
    service = RefundService(db, bot, settings)
    await service.request(100, payment.id, "refund")
    await assert_balanced(db, 0)
    await service.process(payment.id)
    await service.process(payment.id)
    await assert_balanced(db, 0)
    bot.refund_star_payment.assert_awaited_once()


async def test_spent_topup_manual_refund_refused(db, settings, seeded, bot):
    _, _, payment = await topup(db, settings)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "buy")
    await service.purchase(101, order.id)
    with pytest.raises(Denied):
        await RefundService(db, bot, settings).request(100, payment.id, "refund")
    await assert_balanced(db, 250)


async def test_external_refund_revokes_financed_purchase_and_preserves_other_funds(db, settings, seeded, bot):
    _, data, _ = await topup(db, settings, 100)
    await topup(db, settings, 500)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "buy")
    await service.purchase(101, order.id)
    assert await service.balance(101) == 350
    refund = RefundedPayment(**data.model_dump(exclude_none=True))
    await RefundService(db, bot, settings).external(101, refund)
    await RefundService(db, bot, settings).external(101, refund)
    await assert_balanced(db, 500)
    async with db.sessions() as s:
        assert (await s.get(Order, order.id)).status == "refunded"
        assert (await s.scalar(select(Entitlement))).status == "refunded"


async def test_reversal_before_topup_never_credits(db, settings, seeded, bot):
    service = WalletService(db, settings)
    order = await service.create_topup(101, 500, "topup")
    data = dict(
        currency="XTR",
        total_amount=500,
        invoice_payload=str(order.id),
        telegram_payment_charge_id="charge",
        provider_payment_charge_id="",
    )
    await RefundService(db, bot, settings).external(101, RefundedPayment(**data))
    await PaymentService(db).successful(101, SuccessfulPayment(**data))
    await assert_balanced(db, 0)


async def test_balance_purchase_does_not_accept_successful_payment(db, settings, seeded):
    order = await WalletService(db, settings).quote(101, seeded.plans["fixed"].id, "buy")
    data = SuccessfulPayment(
        currency="XTR",
        total_amount=250,
        invoice_payload=str(order.id),
        telegram_payment_charge_id="forged-wallet-invoice",
        provider_payment_charge_id="",
    )
    with pytest.raises(Denied):
        await PaymentService(db).successful(101, data)
    with pytest.raises(Denied):
        await PaymentService(db).precheckout(101, str(order.id), "XTR", 250)
    await assert_balanced(db, 0)


async def test_topup_and_purchase_history_are_separate_in_admin(db, settings, seeded, bot):
    from app.services.admin import AdminService
    from app.services.admin_forms import AdminForms
    from app.services.telegram_access import AccessService
    from types import SimpleNamespace

    _, _, payment = await topup(db, settings)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "buy")
    await service.purchase(101, order.id)
    admin = AdminService(db, AccessService(db, bot, settings), settings)
    stats = await admin.stats(100)
    assert stats["net_stars"] == 500 and stats["wallet_sales"] == 1 and stats["wallet_balances"] == 250
    assert stats["products"][0]["stars"] == 250
    forms = AdminForms(SimpleNamespace(db=db, admin=admin))
    _, label, _ = await forms.record(100, "payment", payment.id)
    assert "Пополнение" in label
    _, label, _ = await forms.record(100, "purchase", order.id)
    assert seeded.product.title in label


async def test_purchase_refund_releases_funding_for_another_purchase(db, settings, seeded):
    await topup(db, settings, 250)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "first")
    await service.purchase(101, order.id)
    await service.refund_purchase(100, order.id, "refund")
    second = await service.quote(101, seeded.plans["fixed"].id, "second")
    await service.purchase(101, second.id)
    await assert_balanced(db, 0)


async def test_refund_and_purchase_race_does_not_spend_reserved_money(db, settings, seeded, bot):
    _, _, payment = await topup(db, settings, 250)
    service = WalletService(db, settings)
    order = await service.quote(101, seeded.plans["fixed"].id, "buy")
    results = await asyncio.gather(
        service.purchase(101, order.id),
        RefundService(db, bot, settings).request(100, payment.id, "refund"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Denied) for r in results) == 1
    await assert_balanced(db, 0)
