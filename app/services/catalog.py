from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.core.security import Denied
from app.db.models import User, Product, Channel, Plan, now


async def ensure_user(db, telegram_user):
    async with db.transaction() as session:
        values = dict(
            telegram_user_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
            last_seen_at=now(),
        )
        stmt = insert(User).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[User.telegram_user_id],
                set_={k: v for k, v in values.items() if k != "telegram_user_id"},
            )
        )
        return await session.scalar(select(User).where(User.telegram_user_id == telegram_user.id))


async def get_user(session, telegram_id, *, allow_blocked=False):
    user = await session.scalar(select(User).where(User.telegram_user_id == telegram_id))
    if not user or (user.is_blocked and not allow_blocked):
        raise Denied("Пользователь недоступен.")
    return user


async def get_plan(session, plan_id):
    plan = await session.get(Plan, plan_id)
    product = await session.get(Product, plan.product_id) if plan else None
    channel = await session.get(Channel, product.channel_id) if product else None
    if not all((plan, product, channel)) or not all((plan.is_active, product.is_active, channel.is_active)):
        raise Denied("Этот тариф больше недоступен.")
    return plan, product


class CatalogService:
    def __init__(self, db):
        self.db = db

    async def page(self, page=0):
        if not 0 <= page <= 100000:
            raise Denied("Некорректная страница.")
        async with self.db.sessions() as s:
            return list(
                await s.scalars(
                    select(Product)
                    .join(Channel)
                    .where(Product.is_active.is_(True), Channel.is_active.is_(True))
                    .order_by(Product.sort_order, Product.id)
                    .offset(page * 8)
                    .limit(9)
                )
            )

    async def product(self, product_id, page=0):
        if not 0 <= page <= 100000:
            raise Denied("Некорректная страница.")
        async with self.db.sessions() as s:
            product = await s.get(Product, product_id)
            channel = await s.get(Channel, product.channel_id) if product else None
            if not product or not product.is_active or not channel or not channel.is_active:
                raise Denied("Продукт недоступен.")
            plans = list(
                await s.scalars(
                    select(Plan)
                    .where(
                        Plan.product_id == product_id,
                        Plan.is_active.is_(True),
                        Plan.billing_type != "recurring_30d",
                    )
                    .order_by(Plan.sort_order, Plan.id)
                    .offset(page * 8)
                    .limit(9)
                )
            )
            return product, plans
