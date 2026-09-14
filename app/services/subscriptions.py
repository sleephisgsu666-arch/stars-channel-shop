from sqlalchemy import select
from app.db.models import Order, now
from app.services.catalog import get_user
from app.core.security import Denied, uuid_value


class SubscriptionService:
    def __init__(self, db, bot):
        self.db, self.bot = db, bot

    async def change(self, telegram_id, order_id, canceled):
        if not canceled:
            raise Denied("Автопродление отключено. Покупайте доступ с баланса.")
        async with self.db.lock(f"subscription:{order_id}"):
            async with self.db.sessions() as s:
                user = await get_user(s, telegram_id)
                order = await s.get(Order, order_id)
                if not order or order.user_id != user.id or not order.subscription_charge_id:
                    raise Denied("Подписка не найдена.")
            await self.bot.edit_user_star_subscription(
                telegram_id, order.subscription_charge_id, is_canceled=canceled
            )
            async with self.db.transaction() as s:
                order = await s.get(Order, order_id, with_for_update=True)
                order.subscription_state = "canceled" if canceled else "active"
                order.subscription_updated_at = now()

    async def updated(self, telegram_id, payload, state):
        if state not in ("active", "canceled", "failed"):
            raise Denied("Неизвестное состояние подписки.")
        order_id = uuid_value(payload)
        async with self.db.lock(f"subscription:{order_id}"), self.db.transaction() as s:
            user = await get_user(s, telegram_id, allow_blocked=True)
            order = await s.scalar(select(Order).where(Order.id == order_id).with_for_update())
            if not order or order.user_id != user.id or order.billing_type != "recurring_30d":
                raise Denied("Подписка не найдена.")
            order.subscription_state, order.subscription_updated_at = state, now()
            # State changes never create or extend entitlements.
