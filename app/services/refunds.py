from sqlalchemy import select, func
from aiogram.exceptions import TelegramBadRequest
from app.core.security import authorized, Denied, uuid_value
from app.core.logging import event
from app.db.models import Payment, Order, User, RefundRequest, AuditLog, Grant, Entitlement, now
from app.services.entitlements import refresh
from app.db.models import PaymentReversal
from app.services.wallet import WalletService


class RefundService:
    def __init__(self, db, bot, settings):
        self.db, self.bot, self.settings = db, bot, settings

    async def request(self, admin_id, payment_id, key):
        authorized(admin_id, self.settings.admin_telegram_ids)
        async with self.db.sessions() as s:
            target = await s.get(Payment, payment_id)
            order = await s.get(Order, target.order_id) if target else None
        if order and order.funding_source == "topup":
            return await WalletService(self.db, self.settings).request_topup_refund(admin_id, payment_id, key)
        async with self.db.lock(f"refund:{payment_id}"), self.db.transaction() as s:
            payment = await s.get(Payment, payment_id)
            if not payment:
                raise Denied("Платёж не найден.")
            request = await s.scalar(select(RefundRequest).where(RefundRequest.payment_id == payment_id))
            if not request and payment.status != "refunded":
                s.add(RefundRequest(payment_id=payment_id, admin_telegram_user_id=admin_id))
                s.add(
                    AuditLog(
                        admin_telegram_user_id=admin_id,
                        action="refund_requested",
                        entity_type="payment",
                        entity_id=str(payment_id),
                        request_key=key,
                        old_data={"status": payment.status},
                        new_data={"refund": "pending"},
                    )
                )

    async def process(self, payment_id):
        async with self.db.lock(f"refund:{payment_id}"):
            async with self.db.sessions() as s:
                payment = await s.get(Payment, payment_id)
                if payment.status == "refunded":
                    async with self.db.transaction() as done_session:
                        request = await done_session.scalar(
                            select(RefundRequest).where(RefundRequest.payment_id == payment_id)
                        )
                        if request:
                            request.status = "done"
                    return
                user = await s.get(User, payment.user_id)
                order = await s.get(Order, payment.order_id)
            if order.funding_source == "topup":
                async with self.db.lock(f"wallet:{user.telegram_user_id}"):
                    async with self.db.transaction() as s:
                        request = await s.scalar(
                            select(RefundRequest).where(RefundRequest.payment_id == payment_id)
                        )
                        if not request:
                            raise Denied("Возврат не запрошен.")
                        request.attempts += 1
                    try:
                        await self.bot.refund_star_payment(
                            user.telegram_user_id, payment.telegram_payment_charge_id
                        )
                    except TelegramBadRequest as exc:
                        if "CHARGE_ALREADY_REFUNDED" not in exc.message:
                            raise
                    await WalletService(self.db).reverse_topup(user.telegram_user_id, payment_id)
                return
            async with self.db.lock(f"access:{user.telegram_user_id}:{order.product_id}"):
                async with self.db.transaction() as s:
                    request = await s.scalar(
                        select(RefundRequest).where(RefundRequest.payment_id == payment_id)
                    )
                    if not request:
                        raise Denied("Возврат не запрошен.")
                    request.attempts += 1
                if order.subscription_charge_id and order.subscription_state != "canceled":
                    await self.bot.edit_user_star_subscription(
                        user.telegram_user_id, order.subscription_charge_id, is_canceled=True
                    )
                    async with self.db.transaction() as state_session:
                        stored_order = await state_session.get(Order, order.id)
                        stored_order.subscription_state = "canceled"
                try:
                    await self.bot.refund_star_payment(
                        user.telegram_user_id, payment.telegram_payment_charge_id
                    )
                except TelegramBadRequest as exc:
                    # Telegram's explicit already-refunded result makes crash recovery idempotent.
                    if "CHARGE_ALREADY_REFUNDED" not in exc.message:
                        raise
                await self.finalize(payment_id)

    async def finalize(self, payment_id):
        async with self.db.transaction() as s:
            payment = await s.get(Payment, payment_id, with_for_update=True)
            if payment.status == "refunded":
                return
            payment.status, payment.refunded_at = "refunded", now()
            order = await s.get(Order, payment.order_id, with_for_update=True)
            await s.flush()
            remaining = await s.scalar(
                select(func.count())
                .select_from(Payment)
                .where(Payment.order_id == order.id, Payment.status == "paid")
            )
            order.status = "paid" if remaining else "refunded"
            grant = await s.scalar(select(Grant).where(Grant.payment_id == payment.id))
            if grant:
                grant.revoked = True
                await s.flush()
                ent = await s.get(Entitlement, grant.entitlement_id, with_for_update=True)
                await refresh(s, ent)
            request = await s.scalar(select(RefundRequest).where(RefundRequest.payment_id == payment_id))
            if request:
                request.status = "done"
                s.add(
                    AuditLog(
                        admin_telegram_user_id=request.admin_telegram_user_id,
                        action="refund_success",
                        entity_type="payment",
                        entity_id=str(payment_id),
                        request_key=f"refund-done:{payment_id}",
                        old_data={"status": "paid"},
                        new_data={"status": "refunded"},
                    )
                )
            event("refund_success", payment_id=payment_id)

    async def external(self, telegram_id, data):
        order_id = uuid_value(data.invoice_payload)
        async with self.db.sessions() as s:
            order = await s.get(Order, order_id)
            user = await s.get(User, order.user_id) if order else None
            if (
                not order
                or not user
                or user.telegram_user_id != telegram_id
                or order.amount_stars != data.total_amount
                or data.currency != "XTR"
            ):
                raise Denied("Несоответствие возврата.")
        lock = (
            f"wallet:{telegram_id}"
            if order.funding_source == "topup"
            else f"access:{telegram_id}:{order.product_id}"
        )
        async with self.db.lock(lock):
            async with self.db.transaction() as s:
                reversal = await s.scalar(
                    select(PaymentReversal).where(
                        PaymentReversal.telegram_payment_charge_id == data.telegram_payment_charge_id
                    )
                )
                if reversal and reversal.order_id != order.id:
                    raise Denied("Несоответствие возврата.")
                if not reversal:
                    s.add(
                        PaymentReversal(
                            order_id=order.id,
                            user_id=user.id,
                            telegram_payment_charge_id=data.telegram_payment_charge_id,
                            amount_stars=data.total_amount,
                            currency=data.currency,
                        )
                    )
                payment = await s.scalar(
                    select(Payment).where(
                        Payment.telegram_payment_charge_id == data.telegram_payment_charge_id
                    )
                )
                if payment and payment.order_id != order.id:
                    raise Denied("Несоответствие возврата.")
            if payment:
                if order.funding_source == "topup":
                    await WalletService(self.db).reverse_topup(telegram_id, payment.id)
                    return
                await self.finalize(payment.id)
                if order.subscription_charge_id:
                    await self.bot.edit_user_star_subscription(
                        telegram_id, order.subscription_charge_id, is_canceled=True
                    )
                    async with self.db.transaction() as s:
                        stored_order = await s.get(Order, order.id)
                        stored_order.subscription_state = "canceled"
