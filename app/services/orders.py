from __future__ import annotations

from uuid import UUID
from app.db.session import Database
from app.core.config import Settings
from app.db.models import Entitlement
from datetime import timedelta
from sqlalchemy import select
from app.db.models import Order, now
from app.services.catalog import get_plan, get_user
from app.services.entitlements import grant_access
from app.core.security import Denied
from app.core.logging import event


class OrderService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings

    async def create(self, telegram_id: int, plan_id: UUID, request_key: str) -> Order:
        async with self.db.sessions() as s:
            plan, product = await get_plan(s, plan_id)
        async with self.db.lock(f"access:{telegram_id}:{product.id}"), self.db.transaction() as s:
            user = await get_user(s, telegram_id)
            plan, product = await get_plan(s, plan_id)
            old = await s.scalar(select(Order).where(Order.request_key == request_key))
            if old:
                if old.user_id != user.id or old.plan_id != plan.id:
                    raise Denied("Некорректный заказ.")
                return old
            if plan.billing_type == "free":
                raise Denied("Для бесплатного тарифа счёт не нужен.")
            order = Order(
                user_id=user.id,
                product_id=product.id,
                plan_id=plan.id,
                amount_stars=plan.price_stars,
                billing_type=plan.billing_type,
                duration_days=plan.duration_days,
                request_key=request_key,
                expires_at=now() + timedelta(seconds=self.settings.order_ttl),
            )
            s.add(order)
            await s.flush()
            event("invoice_created", order_id=order.id, telegram_user_id=telegram_id)
            return order

    async def free(self, telegram_id: int, plan_id: UUID) -> Entitlement:
        async with self.db.sessions() as s:
            plan, product = await get_plan(s, plan_id)
        async with self.db.lock(f"access:{telegram_id}:{product.id}"), self.db.transaction() as s:
            user = await get_user(s, telegram_id)
            plan, product = await get_plan(s, plan_id)
            if plan.billing_type != "free" or plan.price_stars != 0:
                raise Denied("Требуется оплата.")
            return await grant_access(
                s, user.id, product.id, plan.id, f"free:{user.id}:{plan.id}", "free", plan.duration_days
            )
