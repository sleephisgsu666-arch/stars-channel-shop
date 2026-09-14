"""Stars top-ups and prepaid purchases. All money changes are serialized per user."""

from contextlib import AsyncExitStack
from datetime import timedelta
from uuid import UUID
from sqlalchemy import select, func
from app.core.security import Denied, InsufficientBalance, authorized
from app.db.models import (
    User,
    Order,
    Payment,
    PaymentReversal,
    WalletEntry,
    WalletAllocation,
    Grant,
    Entitlement,
    RefundRequest,
    AuditLog,
    now,
)
from app.services.catalog import get_user, get_plan
from app.services.entitlements import grant_access, refresh
from app.core.logging import event


class WalletService:
    TOPUPS = (100, 250, 500, 1000, 2500, 5000)

    def __init__(self, db, settings=None):
        self.db, self.settings = db, settings

    async def balance(self, uid):
        async with self.db.sessions() as s:
            return (await get_user(s, uid)).balance_stars

    async def create_topup(self, uid, amount, key):
        if type(amount) is not int or not 1 <= amount <= 100000:
            raise Denied("Введите от 1 до 100000 Stars.")
        async with self.db.lock(f"wallet:{uid}"), self.db.transaction() as s:
            user = await get_user(s, uid)
            existing = await s.scalar(select(Order).where(Order.request_key == key))
            if existing:
                if (
                    existing.user_id != user.id
                    or existing.funding_source != "topup"
                    or existing.amount_stars != amount
                ):
                    raise Denied("Некорректное пополнение.")
                return existing
            order = Order(
                user_id=user.id,
                product_id=None,
                plan_id=None,
                funding_source="topup",
                billing_type="topup",
                amount_stars=amount,
                request_key=key,
                expires_at=now() + timedelta(minutes=30),
            )
            s.add(order)
            await s.flush()
            return order

    async def precheckout(self, uid, order_id, currency, amount):
        async with self.db.transaction() as s:
            user = await get_user(s, uid)
            order = await s.get(Order, order_id, with_for_update=True)
            self.validate(order, user, currency, amount)
            if order.status not in ("pending", "precheckout_approved") or order.expires_at <= now():
                raise Denied("Счёт на пополнение истёк. Создайте новый.")
            order.status = "precheckout_approved"

    @staticmethod
    def validate(order, user, currency, amount):
        if (
            not order
            or order.funding_source != "topup"
            or order.user_id != user.id
            or currency != "XTR"
            or amount != order.amount_stars
        ):
            raise Denied("Платёж не соответствует пополнению.")

    async def successful(self, uid, payment):
        async with self.db.lock(f"wallet:{uid}"), self.db.transaction() as s:
            user = await get_user(s, uid, allow_blocked=True)
            user = await s.get(User, user.id, with_for_update=True)
            order = await s.get(Order, UUID(payment.invoice_payload), with_for_update=True)
            self.validate(order, user, payment.currency, payment.total_amount)
            if payment.is_recurring or payment.is_first_recurring:
                raise Denied("Пополнение не поддерживает автопродление.")
            existing = await s.scalar(
                select(Payment).where(
                    Payment.telegram_payment_charge_id == payment.telegram_payment_charge_id
                )
            )
            if existing:
                if existing.order_id != order.id or existing.user_id != user.id:
                    raise Denied("Несоответствие платежа.")
                return user.balance_stars
            record = Payment(
                order_id=order.id,
                user_id=user.id,
                amount_stars=payment.total_amount,
                currency="XTR",
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                provider_payment_charge_id=payment.provider_payment_charge_id,
            )
            s.add(record)
            await s.flush()
            reversed_payment = await s.scalar(
                select(PaymentReversal).where(
                    PaymentReversal.telegram_payment_charge_id == record.telegram_payment_charge_id
                )
            )
            if reversed_payment:
                if reversed_payment.order_id != order.id:
                    raise Denied("Несоответствие возврата.")
                record.status, record.refunded_at = "refunded", now()
                if order.status != "paid":
                    order.status = "refunded"
            else:
                # Every distinct verified charge credits its real amount, including duplicate invoices.
                user.balance_stars += record.amount_stars
                s.add(
                    WalletEntry(
                        user_id=user.id,
                        order_id=order.id,
                        payment_id=record.id,
                        amount=record.amount_stars,
                        kind="topup",
                        idempotency_key=f"topup:{record.id}",
                    )
                )
                order.status, order.paid_at = "paid", order.paid_at or now()
            event("wallet_topup", order_id=order.id)
            return user.balance_stars

    async def quote(self, uid, plan_id, key):
        async with self.db.lock(f"wallet:{uid}"), self.db.transaction() as s:
            user = await get_user(s, uid)
            plan, product = await get_plan(s, plan_id)
            if plan.billing_type not in ("fixed", "lifetime"):
                raise Denied("Выберите разовый доступ на срок или навсегда.")
            old = await s.scalar(select(Order).where(Order.request_key == key))
            if old:
                if old.user_id != user.id or old.plan_id != plan_id or old.funding_source != "wallet":
                    raise Denied("Некорректная покупка.")
                return old
            order = Order(
                user_id=user.id,
                product_id=product.id,
                plan_id=plan.id,
                amount_stars=plan.price_stars,
                billing_type=plan.billing_type,
                duration_days=plan.duration_days,
                funding_source="wallet",
                request_key=key,
                expires_at=now() + timedelta(minutes=30),
            )
            s.add(order)
            await s.flush()
            return order

    async def purchase(self, uid, order_id):
        async with self.db.sessions() as s:
            order = await s.get(Order, order_id)
            if not order or order.funding_source != "wallet":
                raise Denied("Покупка не найдена.")
            product_id = order.product_id
        async with (
            self.db.lock(f"wallet:{uid}"),
            self.db.lock(f"access:{uid}:{product_id}"),
            self.db.transaction() as s,
        ):
            user = await get_user(s, uid)
            user = await s.get(User, user.id, with_for_update=True)
            order = await s.get(Order, order_id, with_for_update=True)
            if order.user_id != user.id:
                raise Denied("Покупка принадлежит другому пользователю.")
            if order.status == "paid":
                return await s.scalar(
                    select(Entitlement).where(
                        Entitlement.user_id == user.id, Entitlement.product_id == product_id
                    )
                )
            if order.status != "pending" or order.expires_at <= now():
                raise Denied("Покупка закрыта. Выберите тариф заново.")
            await get_plan(s, order.plan_id)
            if order.billing_type not in ("fixed", "lifetime"):
                raise Denied("Автопродление отключено.")
            if user.balance_stars < order.amount_stars:
                raise InsufficientBalance(order.amount_stars - user.balance_stars)
            sources = list(
                await s.scalars(
                    select(Payment)
                    .join(Order)
                    .where(
                        Payment.user_id == user.id, Payment.status == "paid", Order.funding_source == "topup"
                    )
                    .order_by(Payment.created_at, Payment.id)
                )
            )
            remaining = order.amount_stars
            for source in sources:
                if await s.scalar(
                    select(RefundRequest.id).where(
                        RefundRequest.payment_id == source.id, RefundRequest.status == "pending"
                    )
                ):
                    continue
                spent = await s.scalar(
                    select(func.coalesce(func.sum(WalletAllocation.amount), 0))
                    .join(Order, Order.id == WalletAllocation.purchase_order_id)
                    .where(WalletAllocation.payment_id == source.id, Order.status == "paid")
                )
                take = min(remaining, source.amount_stars - spent)
                if take > 0:
                    s.add(WalletAllocation(purchase_order_id=order.id, payment_id=source.id, amount=take))
                    remaining -= take
                if remaining == 0:
                    break
            if remaining:
                raise Denied("Часть средств ожидает возврата. Попробуйте позже.")
            user.balance_stars -= order.amount_stars
            order.status, order.paid_at = "paid", now()
            s.add(
                WalletEntry(
                    user_id=user.id,
                    order_id=order.id,
                    amount=-order.amount_stars,
                    kind="purchase",
                    idempotency_key=f"purchase:{order.id}",
                )
            )
            ent = await grant_access(
                s,
                user.id,
                product_id,
                order.plan_id,
                f"wallet:{order.id}",
                order.billing_type,
                order.duration_days,
                order_id=order.id,
            )
            grant = await s.scalar(select(Grant).where(Grant.idempotency_key == f"wallet:{order.id}"))
            grant.purchase_order_id = order.id
            return ent

    async def request_topup_refund(self, admin_id, payment_id, key):
        authorized(admin_id, self.settings.admin_telegram_ids)
        async with self.db.sessions() as s:
            payment = await s.get(Payment, payment_id)
            user = await s.get(User, payment.user_id)
        async with self.db.lock(f"wallet:{user.telegram_user_id}"), self.db.transaction() as s:
            payment = await s.get(Payment, payment_id, with_for_update=True)
            user = await s.get(User, user.id, with_for_update=True)
            if payment.status == "refunded" or await s.scalar(
                select(RefundRequest).where(RefundRequest.payment_id == payment_id)
            ):
                return
            used = await s.scalar(
                select(func.count())
                .select_from(WalletAllocation)
                .join(Order, Order.id == WalletAllocation.purchase_order_id)
                .where(WalletAllocation.payment_id == payment_id, Order.status == "paid")
            )
            if used or user.balance_stars < payment.amount_stars:
                raise Denied(
                    "Пополнение уже потрачено. Сначала верните связанные покупки на баланс через раздел «Покупки»."
                )
            user.balance_stars -= payment.amount_stars
            s.add(
                WalletEntry(
                    user_id=user.id,
                    order_id=payment.order_id,
                    payment_id=payment_id,
                    amount=-payment.amount_stars,
                    kind="refund_reserve",
                    idempotency_key=f"reserve:{payment_id}",
                )
            )
            s.add(RefundRequest(payment_id=payment_id, admin_telegram_user_id=admin_id))
            s.add(
                AuditLog(
                    admin_telegram_user_id=admin_id,
                    action="topup_refund_requested",
                    entity_type="payment",
                    entity_id=str(payment_id),
                    request_key=key,
                    old_data={"status": "paid"},
                    new_data={"refund": "reserved"},
                )
            )

    async def refund_purchase_in_session(self, s, user, order):
        if order.status != "paid":
            return
        order.status = "refunded"
        user.balance_stars += order.amount_stars
        s.add(
            WalletEntry(
                user_id=user.id,
                order_id=order.id,
                amount=order.amount_stars,
                kind="purchase_refund",
                idempotency_key=f"purchase-refund:{order.id}",
            )
        )
        grant = await s.scalar(select(Grant).where(Grant.purchase_order_id == order.id))
        if grant:
            grant.revoked = True
            await s.flush()
            ent = await s.get(Entitlement, grant.entitlement_id, with_for_update=True)
            await refresh(s, ent)

    async def refund_purchase(self, admin_id, order_id, key):
        authorized(admin_id, self.settings.admin_telegram_ids)
        async with self.db.sessions() as s:
            order = await s.get(Order, order_id)
            if not order or order.funding_source != "wallet":
                raise Denied("Покупка с баланса не найдена.")
            user = await s.get(User, order.user_id)
        async with (
            self.db.lock(f"wallet:{user.telegram_user_id}"),
            self.db.lock(f"access:{user.telegram_user_id}:{order.product_id}"),
            self.db.transaction() as s,
        ):
            user = await s.get(User, user.id, with_for_update=True)
            order = await s.get(Order, order_id, with_for_update=True)
            if order.status == "refunded":
                return
            if order.status != "paid":
                raise Denied("Покупка не оплачена.")
            await self.refund_purchase_in_session(s, user, order)
            s.add(
                AuditLog(
                    admin_telegram_user_id=admin_id,
                    action="wallet_purchase_refund",
                    entity_type="order",
                    entity_id=str(order.id),
                    request_key=key,
                    old_data={"status": "paid"},
                    new_data={"status": "refunded"},
                )
            )

    async def reverse_topup(self, uid, payment_id):
        # Caller holds wallet lock. Lock every affected product before opening a money transaction.
        async with self.db.sessions() as s:
            purchases = list(
                await s.scalars(
                    select(Order)
                    .join(WalletAllocation, WalletAllocation.purchase_order_id == Order.id)
                    .where(WalletAllocation.payment_id == payment_id, Order.status == "paid")
                )
            )
        async with AsyncExitStack() as locks:
            for product_id in sorted({o.product_id for o in purchases}, key=str):
                await locks.enter_async_context(self.db.lock(f"access:{uid}:{product_id}"))
            async with self.db.transaction() as s:
                payment = await s.get(Payment, payment_id, with_for_update=True)
                if payment.status == "refunded":
                    return
                user = await s.get(User, payment.user_id, with_for_update=True)
                for old in purchases:
                    order = await s.get(Order, old.id, with_for_update=True)
                    await self.refund_purchase_in_session(s, user, order)
                reserved = await s.scalar(
                    select(WalletEntry).where(WalletEntry.idempotency_key == f"reserve:{payment.id}")
                )
                if not reserved:
                    user.balance_stars -= payment.amount_stars
                    s.add(
                        WalletEntry(
                            user_id=user.id,
                            order_id=payment.order_id,
                            payment_id=payment.id,
                            amount=-payment.amount_stars,
                            kind="topup_refund",
                            idempotency_key=f"topup-refund:{payment.id}",
                        )
                    )
                payment.status, payment.refunded_at = "refunded", now()
                await s.flush()
                order = await s.get(Order, payment.order_id)
                remaining = await s.scalar(
                    select(func.count())
                    .select_from(Payment)
                    .where(Payment.order_id == order.id, Payment.status == "paid")
                )
                order.status = "paid" if remaining else "refunded"
                request = await s.scalar(select(RefundRequest).where(RefundRequest.payment_id == payment.id))
                if request:
                    request.status = "done"
                s.add(
                    AuditLog(
                        admin_telegram_user_id=request.admin_telegram_user_id if request else 0,
                        action="topup_refunded",
                        entity_type="payment",
                        entity_id=str(payment.id),
                        request_key=f"topup-reversed:{payment.id}",
                        old_data={"status": "paid"},
                        new_data={"status": "refunded"},
                    )
                )
