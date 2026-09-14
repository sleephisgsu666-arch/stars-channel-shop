from unittest.mock import AsyncMock
from types import SimpleNamespace
from sqlalchemy import select, func
import pytest
from app.services.admin import AdminService
from app.services.admin_forms import AdminForms, new_draft
from app.services.telegram_access import AccessService
from app.bot.handlers import admin_wizard
from app.core.security import Denied
from app.db.models import Product, AuditLog, Plan


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)


def runtime(db, settings, bot):
    access = AccessService(db, bot, settings)
    return SimpleNamespace(
        db=db,
        redis=MemoryRedis(),
        admin=AdminService(db, access, settings),
        limiter=SimpleNamespace(check=AsyncMock()),
    )


def message(text=None, mid=10):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        text=text,
        message_id=mid,
        photo=None,
        forward_origin=None,
        answer=AsyncMock(),
    )


async def click(r, data):
    q = SimpleNamespace(
        from_user=SimpleNamespace(id=100), message=message(mid=1), data=data, answer=AsyncMock()
    )
    await admin_wizard.callback(q, r)


async def choice(r, action, value=""):
    draft = await AdminForms(r).load(100)
    await click(r, admin_wizard.button(draft["token"], action, value))


async def test_product_wizard_saves_only_after_confirmation(db, settings, seeded, bot):
    # Existing seeded product uses channel; create a new channel for this product.
    from app.db.models import Channel

    async with db.transaction() as s:
        channel = Channel(telegram_chat_id=-1007777777777, title="Второй канал")
        s.add(channel)
        await s.flush()
    r = runtime(db, settings, bot)
    await click(r, "anew:product")
    await choice(r, "pick", str(channel.id))
    await admin_wizard.message(message("Новый продукт", 20), r)
    await admin_wizard.message(message("Описание", 21), r)
    await choice(r, "empty")
    await choice(r, "value", "yes")
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Product)) == 1
    await choice(r, "save")
    async with db.sessions() as s:
        product = await s.scalar(select(Product).where(Product.channel_id == channel.id))
        assert product.title == "Новый продукт" and product.description == "Описание" and product.is_active
        assert await s.scalar(select(func.count()).select_from(AuditLog)) == 1
    bot.get_chat.assert_awaited()


async def test_edit_preserves_other_fields_and_detects_conflict(db, settings, seeded, bot):
    r = runtime(db, settings, bot)
    forms = AdminForms(r)
    _, _, data = await forms.record(100, "product", seeded.product.id)
    draft = new_draft("product", str(seeded.product.id), data, "title")
    await forms.put(100, draft)
    await admin_wizard.message(message("Новое название"), r)
    # A second admin changes the record while first admin reviews the confirmation.
    async with db.transaction() as s:
        product = await s.get(Product, seeded.product.id)
        product.description = "Изменено другим администратором"
    with pytest.raises(Denied, match="уже изменена"):
        await choice(r, "save")
    async with db.sessions() as s:
        product = await s.get(Product, seeded.product.id)
        assert product.title == "Product" and product.description == "Изменено другим администратором"
    # Start a fresh form and save a single field without resetting other fields.
    _, _, data = await forms.record(100, "product", seeded.product.id)
    await forms.put(100, new_draft("product", str(seeded.product.id), data, "title"))
    await admin_wizard.message(message("Новое название", 21), r)
    await choice(r, "save")
    async with db.sessions() as s:
        product = await s.get(Product, seeded.product.id)
        assert product.title == "Новое название" and product.description == "Изменено другим администратором"


async def test_fixed_form_and_change_to_free(db, settings, seeded, bot):
    r = runtime(db, settings, bot)
    await click(r, "anew:plan")
    await choice(r, "pick", str(seeded.product.id))
    await admin_wizard.message(message("Новая подписка", 20), r)
    await choice(r, "value", "fixed")
    await admin_wizard.message(message("500", 22), r)
    await admin_wizard.message(message("30", 23), r)
    await choice(r, "value", "yes")
    await choice(r, "save")
    async with db.sessions() as s:
        plan = await s.scalar(select(Plan).where(Plan.name == "Новая подписка"))
        assert plan.duration_days == 30 and plan.price_stars == 500
    field_index = admin_wizard.EDIT["plan"].index("billing_type")
    await click(r, f"aedit:plan:{plan.id}:{field_index}")
    await choice(r, "value", "free")
    await choice(r, "forever")
    await choice(r, "save")
    async with db.sessions() as s:
        plan = await s.get(Plan, plan.id)
        assert plan.billing_type == "free" and plan.duration_days is None and plan.price_stars == 0
