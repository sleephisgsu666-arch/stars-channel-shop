"""Server-owned admin drafts. Values from buttons are reloaded before saving."""

import json
import secrets
from uuid import UUID
from app.core.security import Denied
from app.db.models import User, Product, Channel, Order

LABELS = {
    "channel": "Каналы",
    "product": "Продукты",
    "plan": "Тарифы",
    "user": "Пользователи",
    "access": "Доступы",
    "payment": "Платежи / пополнения",
    "purchase": "Покупки с баланса",
    "audit": "Журнал действий",
}
FIELDS = {
    "title": "Название",
    "name": "Название",
    "description": "Описание",
    "short_description": "Краткое описание",
    "cover_file_id": "Обложка",
    "sort_order": "Порядок в списке",
    "price_stars": "Цена в Stars",
    "duration_days": "Срок в днях",
    "billing_type": "Тип тарифа",
    "is_active": "Доступность",
    "telegram_chat_id": "ID канала",
    "telegram_user_id": "Пользователь",
    "_balance": "Баланс",
    "days": "Срок доступа",
    "product_id": "Продукт",
    "channel_id": "Канал",
}
TYPES = {
    "free": "Бесплатный",
    "fixed": "Доступ на срок (без автопродления)",
    "lifetime": "Навсегда",
}
STATUS = {
    "active": "Активен",
    "expired": "Истёк",
    "revoked": "Отозван",
    "refunded": "Возвращён",
    "paid": "Оплачен",
}


class AdminForms:
    def __init__(self, runtime):
        self.r = runtime

    def key(self, uid):
        return f"admin-form:{uid}"

    async def load(self, uid):
        self.r.admin.auth(uid)
        raw = await self.r.redis.get(self.key(uid))
        return json.loads(raw) if raw else None

    async def put(self, uid, draft):
        self.r.admin.auth(uid)
        # Rotate on every step so buttons from older prompts cannot change a newer draft.
        draft["token"] = secrets.token_hex(4)
        await self.r.redis.set(self.key(uid), json.dumps(draft, ensure_ascii=False), ex=1800)
        return draft

    async def clear(self, uid):
        self.r.admin.auth(uid)
        await self.r.redis.delete(self.key(uid))

    async def record(self, uid, kind, entity_id):
        self.r.admin.auth(uid)
        if kind not in self.r.admin.LISTS:
            raise Denied("Раздел не найден.")
        async with self.r.db.sessions() as s:
            row = await s.get(self.r.admin.LISTS[kind], UUID(str(entity_id)))
            if not row:
                raise Denied("Запись больше недоступна.")
            if kind in self.r.admin.ENTITIES:
                _, schema = self.r.admin.ENTITIES[kind]
                data = {field: getattr(row, field) for field in schema.model_fields}
                data = json.loads(json.dumps(data, default=str))
                label = getattr(row, "title", None) or getattr(row, "name", "")
                if kind == "product":
                    data["_channel"] = (await s.get(Channel, row.channel_id)).title
                if kind == "plan":
                    data["_product"] = (await s.get(Product, row.product_id)).title
                return str(row.id), label, data
            if kind == "user":
                label = " ".join(filter(None, [row.first_name, row.last_name])) or str(row.telegram_user_id)
                return (
                    str(row.id),
                    label,
                    {"telegram_user_id": row.telegram_user_id, "_balance": row.balance_stars},
                )
            if kind in ("access", "payment", "purchase"):
                user = await s.get(User, row.user_id)
                product_id = (
                    row.product_id
                    if kind in ("access", "purchase")
                    else (await s.get(Order, row.order_id)).product_id
                )
                product = await s.get(Product, product_id) if product_id else None
                title = product.title if product else "Пополнение баланса"
                data = {
                    "telegram_user_id": user.telegram_user_id,
                    "product_id": str(product_id),
                    "_product": title,
                    "status": row.status,
                }
                if kind in ("payment", "purchase"):
                    data["amount_stars"] = row.amount_stars
                else:
                    data["expires_at"] = row.expires_at.isoformat() if row.expires_at else "Навсегда"
                return str(row.id), f"{title} · {user.telegram_user_id}", data
            return (
                str(row.id),
                row.action,
                {
                    "Администратор": row.admin_telegram_user_id,
                    "Действие": row.action,
                    "Тип": row.entity_type,
                    "Когда": row.created_at.isoformat(),
                },
            )

    async def page(self, uid, kind, page):
        rows = await self.r.admin.listing(uid, kind, page)
        return [await self.record(uid, kind, row["id"]) for row in rows]


def clean(data):
    return {key: value for key, value in data.items() if not key.startswith("_")}


def steps(kind):
    return {
        "channel": ["telegram_chat_id", "title"],
        "product": ["channel_id", "title", "description", "cover_file_id", "is_active"],
        "plan": ["product_id", "name", "billing_type", "price_stars", "duration_days", "is_active"],
        "access": ["telegram_user_id", "product_id", "days"],
        "refund": [],
        "purchase_refund": [],
    }[kind]


def next_step(draft):
    sequence = draft["remaining"]
    data = draft["data"]
    while sequence:
        field = sequence.pop(0)
        if draft["kind"] == "plan":
            if field == "price_stars" and data.get("billing_type") == "free":
                data["price_stars"] = 0
                continue
            if field == "duration_days" and data.get("billing_type") in ("lifetime", "recurring_30d"):
                data["duration_days"] = 30 if data["billing_type"] == "recurring_30d" else None
                continue
        draft["step"] = field
        return
    draft["step"] = "confirm"


def new_draft(kind, entity_id=None, data=None, field=None):
    defaults = {
        "channel": {"is_active": True},
        "product": {
            "is_active": False,
            "sort_order": 0,
            "short_description": "",
            "description": "",
            "cover_file_id": None,
        },
        "plan": {"is_active": True, "sort_order": 0},
        "access": {"days": None, "revoke": False},
        "refund": {},
        "purchase_refund": {},
    }
    if kind not in defaults:
        raise Denied("Раздел не найден.")
    draft = {
        "kind": kind,
        "entity_id": entity_id,
        "data": data or defaults[kind],
        "request_key": "form:" + secrets.token_hex(16),
        "last_message": 0,
        "baseline": clean(data) if entity_id and kind in ("channel", "product", "plan") else None,
        "remaining": [field] if field else steps(kind),
    }
    if field == "billing_type":
        draft["remaining"] += ["price_stars", "duration_days"]
    next_step(draft)
    return draft


def parse_input(field, text, photo=None):
    text = (text or "").strip()
    if field == "cover_file_id":
        if photo:
            return photo[-1].file_id
        raise Denied("Отправьте фотографию или нажмите «Без обложки».")
    if field in (
        "telegram_chat_id",
        "telegram_user_id",
        "price_stars",
        "duration_days",
        "days",
        "sort_order",
    ):
        try:
            value = int(text)
        except ValueError:
            raise Denied("Введите целое число без дополнительных символов.") from None
        bounds = {
            "telegram_chat_id": (-(2**63), -1),
            "telegram_user_id": (1, 2**63 - 1),
            "price_stars": (1, 1000000),
            "duration_days": (1, 36500),
            "days": (1, 36500),
            "sort_order": (-100000, 100000),
        }
        low, high = bounds[field]
        if not low <= value <= high:
            raise Denied(f"Допустимое значение: от {low} до {high}.")
        return value
    limit = {"title": 128, "name": 128, "description": 3000, "short_description": 500}.get(field)
    if limit is None or not text or len(text) > limit:
        raise Denied(f"Введите текст длиной от 1 до {limit or 128} символов.")
    return text
