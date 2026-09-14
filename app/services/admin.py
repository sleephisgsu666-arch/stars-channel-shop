import json
from datetime import timedelta
from sqlalchemy import select, func, union_all
from app.db.models import Channel, Product, Plan, User, Entitlement, Payment, Order, AuditLog, Grant, now
from app.schemas.admin import ChannelInput, ProductInput, PlanInput, AccessInput
from app.services.entitlements import grant_access
from app.core.security import Denied, authorized


class AdminService:
    ENTITIES = {
        "channel": (Channel, ChannelInput),
        "product": (Product, ProductInput),
        "plan": (Plan, PlanInput),
    }
    LISTS = {
        "channel": Channel,
        "product": Product,
        "plan": Plan,
        "user": User,
        "access": Entitlement,
        "payment": Payment,
        "purchase": Order,
        "audit": AuditLog,
    }

    def __init__(self, db, access, settings):
        self.db, self.access, self.settings = db, access, settings

    def auth(self, telegram_id):
        authorized(telegram_id, self.settings.admin_telegram_ids)

    async def save(self, admin_id, kind, entity_id, raw_json, key, expected_data=None):
        self.auth(admin_id)
        if kind not in self.ENTITIES or len(raw_json) > 16000:
            raise Denied("Некорректные данные.")
        model, schema = self.ENTITIES[kind]
        data = schema.model_validate_json(raw_json)
        values = data.model_dump()
        async with self.db.lock("admin:catalog"):
            async with self.db.sessions() as s:
                previous = await s.scalar(select(AuditLog).where(AuditLog.request_key == key))
                if previous:
                    return previous.entity_id
                old = await s.get(model, entity_id) if entity_id else None
                if entity_id and not old:
                    raise Denied("Объект не найден.")
                if old and expected_data is not None:
                    current = json.loads(
                        json.dumps({field: getattr(old, field) for field in schema.model_fields}, default=str)
                    )
                    if current != expected_data:
                        raise Denied("Запись уже изменена. Отмените редактирование и откройте её заново.")
                if kind == "channel" and old and old.telegram_chat_id != data.telegram_chat_id:
                    raise Denied("ID существующего канала неизменяем; создайте новый канал.")
                if kind == "product":
                    channel = await s.get(Channel, data.channel_id)
                    if not channel:
                        raise Denied("Канал не найден.")
                    if old and old.channel_id != data.channel_id:
                        raise Denied("Канал продукта неизменяем; создайте новый продукт.")
                if kind == "plan":
                    if not await s.get(Product, data.product_id):
                        raise Denied("Продукт не найден.")
                    if old and old.product_id != data.product_id:
                        raise Denied("Продукт тарифа неизменяем.")
            if kind == "channel":
                await self.access.channel_check(data.telegram_chat_id)
            if kind == "product" and data.is_active:
                await self.access.channel_check(channel.telegram_chat_id)
            async with self.db.transaction() as s:
                entity = await s.get(model, entity_id, with_for_update=True) if entity_id else model()
                before = {k: str(getattr(entity, k)) for k in values} if entity_id else None
                for name, value in values.items():
                    setattr(entity, name, value)
                s.add(entity)
                await s.flush()
                s.add(
                    AuditLog(
                        admin_telegram_user_id=admin_id,
                        action="save",
                        entity_type=kind,
                        entity_id=str(entity.id),
                        request_key=key,
                        old_data=before,
                        new_data=json.loads(data.model_dump_json()),
                    )
                )
                return str(entity.id)

    async def manual(self, admin_id, raw_json, key):
        self.auth(admin_id)
        data = AccessInput.model_validate_json(raw_json)
        async with (
            self.db.lock(f"access:{data.telegram_user_id}:{data.product_id}"),
            self.db.transaction() as s,
        ):
            old = await s.scalar(select(AuditLog).where(AuditLog.request_key == key))
            if old:
                return old.entity_id
            user = await s.scalar(select(User).where(User.telegram_user_id == data.telegram_user_id))
            if not user or not await s.get(Product, data.product_id):
                raise Denied("Пользователь должен выполнить /start; продукт должен существовать.")
            ent = await s.scalar(
                select(Entitlement).where(
                    Entitlement.user_id == user.id, Entitlement.product_id == data.product_id
                )
            )
            before = {"status": ent.status, "expires_at": str(ent.expires_at)} if ent else None
            if data.revoke:
                if not ent:
                    raise Denied("Доступ не найден.")
                ent.status, ent.removal_pending = "revoked", True
                for grant in await s.scalars(select(Grant).where(Grant.entitlement_id == ent.id)):
                    grant.revoked = True
            else:
                ent = await grant_access(
                    s, user.id, data.product_id, None, f"admin:{key}", "manual", data.days
                )
            s.add(
                AuditLog(
                    admin_telegram_user_id=admin_id,
                    action="revoke" if data.revoke else "grant",
                    entity_type="entitlement",
                    entity_id=str(ent.id),
                    request_key=key,
                    old_data=before,
                    new_data={"status": ent.status, "expires_at": str(ent.expires_at)},
                )
            )
            return str(ent.id)

    async def listing(self, admin_id, kind, page=0):
        self.auth(admin_id)
        if kind not in self.LISTS or not 0 <= page <= 100000:
            raise Denied("Некорректный раздел.")
        model = self.LISTS[kind]
        async with self.db.sessions() as s:
            query = select(model)
            if kind == "purchase":
                query = query.where(Order.funding_source == "wallet")
            rows = await s.scalars(
                query.order_by(model.created_at.desc(), model.id).offset(page * 8).limit(8)
            )
            output = []
            for row in rows:
                # Explicit safe columns: no invite URLs or credentials.
                fields = (
                    "id",
                    "title",
                    "name",
                    "telegram_user_id",
                    "status",
                    "amount_stars",
                    "user_id",
                    "product_id",
                    "plan_id",
                    "order_id",
                    "is_active",
                    "action",
                    "entity_id",
                    "admin_telegram_user_id",
                    "expires_at",
                    "created_at",
                )
                output.append({k: str(getattr(row, k)) for k in fields if hasattr(row, k)})
            return output

    async def stats(self, admin_id):
        self.auth(admin_id)
        async with self.db.sessions() as s:
            users = await s.scalar(select(func.count()).select_from(User))
            recent = await s.scalar(
                select(func.count()).select_from(User).where(User.created_at > now() - timedelta(days=1))
            )
            accesses = await s.scalar(
                select(func.count())
                .select_from(Entitlement)
                .where(
                    Entitlement.status == "active",
                    (Entitlement.expires_at.is_(None)) | (Entitlement.expires_at > now()),
                )
            )
            paid, stars = (
                await s.execute(
                    select(func.count(), func.coalesce(func.sum(Payment.amount_stars), 0)).where(
                        Payment.status == "paid"
                    )
                )
            ).one()
            refunded = await s.scalar(
                select(func.count()).select_from(Payment).where(Payment.status == "refunded")
            )
            groups = {}
            for label, column in [("products", Order.product_id), ("plans", Order.plan_id)]:
                sales = union_all(
                    select(column.label("entity_id"), Payment.amount_stars.label("amount"))
                    .select_from(Payment)
                    .join(Order)
                    .where(Payment.status == "paid", Order.funding_source == "legacy"),
                    select(column.label("entity_id"), Order.amount_stars.label("amount")).where(
                        Order.status == "paid", Order.funding_source == "wallet"
                    ),
                ).subquery()
                rows = (
                    await s.execute(
                        select(sales.c.entity_id, func.count(), func.sum(sales.c.amount))
                        .group_by(sales.c.entity_id)
                        .order_by(func.sum(sales.c.amount).desc())
                        .limit(20)
                    )
                ).all()
                groups[label] = [{"id": str(r[0]), "payments": r[1], "stars": r[2]} for r in rows]
            return dict(
                users=users,
                new_24h=recent,
                active_entitlements=accesses,
                paid_payments=paid,
                net_stars=stars,
                refunds=refunded,
                wallet_sales=await s.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.funding_source == "wallet", Order.status == "paid")
                ),
                wallet_balances=int(await s.scalar(select(func.coalesce(func.sum(User.balance_stars), 0)))),
                **groups,
            )
