from __future__ import annotations

from uuid import UUID
from aiogram.types import SuccessfulPayment
from app.db.session import Database
from datetime import datetime, timezone
from sqlalchemy import select
from app.db.models import Order, Payment, Plan, Entitlement, RefundRequest, AuditLog, now
from app.services.catalog import get_user, get_plan
from app.db.models import PaymentReversal
from app.services.entitlements import grant_access
from app.services.wallet import WalletService
from app.core.security import Denied, uuid_value
from app.core.logging import event


class PaymentService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def precheckout(self, telegram_id: int, payload: str, currency: str, amount: int) -> None:
        order_id = uuid_value(payload)
        async with self.db.sessions() as session:
            target = await session.get(Order, order_id)
        if target and target.funding_source == "topup":
            return await WalletService(self.db).precheckout(telegram_id, order_id, currency, amount)
        if target and (target.funding_source == "wallet" or target.billing_type == "recurring_30d"):
            raise Denied("Этот счёт не поддерживается. Пополните баланс через меню бота.")
        async with self.db.transaction() as s:
            order = await s.scalar(select(Order).where(Order.id == order_id).with_for_update())
            user = await get_user(s, telegram_id)
            self.validate(order, user.id, currency, amount)
            await get_plan(s, order.plan_id)
            if order.status not in ("pending", "precheckout_approved") or order.expires_at <= now():
                raise Denied("Этот счёт больше недействителен. Создайте новый заказ.")
            order.status = "precheckout_approved"
            event("precheckout_approved", order_id=order.id)

    @staticmethod
    def validate(order: Order | None, user_id: UUID, currency: str, amount: int) -> None:
        if not order or order.user_id != user_id or currency != "XTR" or amount != order.amount_stars:
            raise Denied("Платёж не соответствует заказу.")

    async def successful(self, telegram_id: int, payment: SuccessfulPayment) -> Entitlement | int | None:
        order_id = uuid_value(payment.invoice_payload)
        async with self.db.sessions() as s:
            order = await s.get(Order, order_id)
            if not order:
                raise Denied("Заказ не найден.")
            product_id = order.product_id
        if order.funding_source == "topup":
            return await WalletService(self.db).successful(telegram_id, payment)
        if order.funding_source == "wallet":
            raise Denied("Покупка с баланса не является счётом Telegram.")
        async with self.db.lock(f"access:{telegram_id}:{product_id}"), self.db.transaction() as s:
            order = await s.scalar(select(Order).where(Order.id == order_id).with_for_update())
            # A confirmed payment is recorded even if the user/product was disabled meanwhile.
            user = await get_user(s, telegram_id, allow_blocked=True)
            self.validate(order, user.id, payment.currency, payment.total_amount)
            plan = await s.get(Plan, order.plan_id)
            if not plan or plan.product_id != order.product_id:
                raise Denied("Несоответствие тарифа.")
            old = await s.scalar(
                select(Payment).where(
                    Payment.telegram_payment_charge_id == payment.telegram_payment_charge_id
                )
            )
            if old:
                if old.order_id != order.id or old.user_id != user.id:
                    raise Denied("Несоответствие платежа.")
                if old.status == "refunded" or await s.scalar(
                    select(RefundRequest).where(RefundRequest.payment_id == old.id)
                ):
                    return None
                event("payment_duplicate", order_id=order.id)
                return await s.scalar(
                    select(Entitlement).where(
                        Entitlement.user_id == user.id, Entitlement.product_id == product_id
                    )
                )
            recurring = order.billing_type == "recurring_30d"
            if recurring != bool(payment.is_recurring) or (not recurring and payment.is_first_recurring):
                raise Denied("Несоответствие типа платежа.")
            period_end = (
                datetime.fromtimestamp(payment.subscription_expiration_date, timezone.utc)
                if payment.subscription_expiration_date is not None
                else None
            )
            if recurring and period_end is None:
                raise Denied("Не указан оплаченный период подписки.")
            refund_required = (not recurring and order.status == "paid") or order.status in (
                "canceled",
                "failed",
                "refunded",
            )
            # Late successful events for expired invoices still represent real money.
            record = Payment(
                order_id=order.id,
                user_id=user.id,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                provider_payment_charge_id=payment.provider_payment_charge_id,
                amount_stars=payment.total_amount,
                currency=payment.currency,
                is_recurring=bool(payment.is_recurring),
                is_first_recurring=bool(payment.is_first_recurring),
                subscription_expiration_date=period_end,
            )
            s.add(record)
            await s.flush()
            reversal = await s.scalar(
                select(PaymentReversal).where(
                    PaymentReversal.telegram_payment_charge_id == record.telegram_payment_charge_id
                )
            )
            if reversal:
                if reversal.order_id != order.id or reversal.user_id != user.id:
                    raise Denied("Несоответствие возврата.")
                record.status, record.refunded_at = "refunded", now()
                # No entitlement may be created from an already-refunded charge.
                if order.status != "paid":
                    order.status = "refunded"
                event("payment_already_refunded", payment_id=record.id)
                return None
            if refund_required:
                # Preserve every verified charge, including payments of duplicated/closed invoices.
                s.add(RefundRequest(payment_id=record.id, admin_telegram_user_id=0))
                s.add(
                    AuditLog(
                        admin_telegram_user_id=0,
                        action="automatic_refund_requested",
                        entity_type="payment",
                        entity_id=str(record.id),
                        request_key=f"auto-refund:{record.id}",
                        old_data={"order_status": order.status},
                        new_data={"refund": "pending"},
                    )
                )
                event("payment_refund_queued", order_id=order.id, payment_id=record.id)
                return None
            order.status, order.paid_at = "paid", order.paid_at or now()
            if recurring:
                if not order.subscription_charge_id or payment.is_first_recurring:
                    order.subscription_charge_id = record.telegram_payment_charge_id
                if order.subscription_state != "canceled":
                    order.subscription_state = "active"
            ent = await grant_access(
                s,
                user.id,
                product_id,
                order.plan_id,
                f"payment:{record.id}",
                order.billing_type,
                order.duration_days,
                period_end,
                record.id,
                order.id,
            )
            event("payment_success", order_id=order.id, payment_id=record.id)
            return ent
