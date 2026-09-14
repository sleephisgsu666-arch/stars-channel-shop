from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from app.services.admin_forms import new_draft, next_step, parse_input, AdminForms
from app.bot.handlers.admin_wizard import callback, message, card
from app.core.security import Denied


class RedisMemory:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex):
        assert ex == 1800
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


class Lock:
    async def __aenter__(self):
        pass

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def runtime():
    def auth(uid):
        if uid != 100:
            raise Denied("Нет прав")

    return SimpleNamespace(
        redis=RedisMemory(),
        admin=SimpleNamespace(auth=auth, save=AsyncMock(), manual=AsyncMock()),
        db=SimpleNamespace(lock=lambda key: Lock()),
        limiter=SimpleNamespace(check=AsyncMock()),
    )


def msg(text=None, mid=20):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        message_id=mid,
        text=text,
        photo=None,
        forward_origin=None,
        answer=AsyncMock(),
    )


def query(data):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=100), message=msg(mid=1), data=data, answer=AsyncMock()
    )


@pytest.mark.parametrize(
    "kind,price,duration", [("free", 0, None), ("lifetime", 100, None), ("recurring_30d", 100, 30)]
)
def test_plan_dependent_fields(kind, price, duration):
    draft = new_draft("plan")
    draft["data"].update(billing_type=kind, price_stars=price)
    draft["remaining"] = ["price_stars", "duration_days", "is_active"]
    next_step(draft)
    if kind != "free":
        assert draft["step"] == "price_stars"
        next_step(draft)
    if kind == "free":
        assert draft["step"] == "duration_days"
    else:
        assert draft["step"] == "is_active"
        assert draft["data"]["duration_days"] == duration
    assert draft["data"]["price_stars"] == price


@pytest.mark.parametrize(
    "field,text",
    [
        ("price_stars", "-1"),
        ("duration_days", "0"),
        ("title", "x" * 129),
        ("telegram_chat_id", "100"),
        ("days", "1;drop table users"),
    ],
)
def test_invalid_input(field, text):
    with pytest.raises(Denied):
        parse_input(field, text)


async def test_stale_button_cannot_modify_new_draft(runtime):
    forms = AdminForms(runtime)
    draft = await forms.put(100, new_draft("channel"))
    old = draft["token"]
    await forms.put(100, draft)
    with pytest.raises(Denied, match="устарела"):
        await callback(query(f"aw:{old}:save"), runtime)
    runtime.admin.save.assert_not_awaited()


async def test_cancel_removes_draft(runtime):
    forms = AdminForms(runtime)
    await forms.put(100, new_draft("channel"))
    await message(msg("/cancel"), runtime)
    assert await forms.load(100) is None


async def test_wrong_input_keeps_step(runtime):
    forms = AdminForms(runtime)
    await forms.put(100, new_draft("channel"))
    with pytest.raises(Denied):
        await message(msg("not a number"), runtime)
    assert (await forms.load(100))["step"] == "telegram_chat_id"


async def test_create_channel_requires_confirmation(runtime, monkeypatch):
    from app.bot.handlers import admin_wizard

    monkeypatch.setattr(admin_wizard, "card", AsyncMock())
    forms = AdminForms(runtime)
    await forms.put(100, new_draft("channel"))
    await message(msg("-1001234567890", 20), runtime)
    await message(msg("Мой канал", 21), runtime)
    draft = await forms.load(100)
    assert draft["step"] == "confirm"
    runtime.admin.save.assert_not_awaited()
    await callback(query(f"aw:{draft['token']}:save"), runtime)
    runtime.admin.save.assert_awaited_once()
    assert await forms.load(100) is None
    assert "-1001234567890" in runtime.admin.save.call_args.args[3]


async def test_replayed_text_does_not_fill_next_field(runtime):
    forms = AdminForms(runtime)
    await forms.put(100, new_draft("channel"))
    await message(msg("-1001234567890", 20), runtime)
    await message(msg("-1001234567890", 20), runtime)
    draft = await forms.load(100)
    assert draft["step"] == "title"
    assert "title" not in draft["data"]


async def test_other_user_cannot_use_form(runtime):
    q = query("anew:channel")
    q.from_user.id = 101
    with pytest.raises(Denied):
        await callback(q, runtime)


async def test_edit_buttons_fit_telegram_limit(runtime, monkeypatch):
    entity_id = str(uuid4())
    monkeypatch.setattr(
        AdminForms, "record", AsyncMock(return_value=(entity_id, "Название", {"description": "Описание"}))
    )
    m = msg()
    await card(m, runtime, 100, "product", entity_id)
    for call in m.answer.call_args_list:
        markup = call.kwargs.get("reply_markup")
        if markup:
            for row in markup.inline_keyboard:
                for btn in row:
                    assert len(btn.callback_data.encode()) <= 64


async def test_photo_sets_cover_without_file_id_input(runtime):
    forms = AdminForms(runtime)
    draft = new_draft("product", str(uuid4()), {"title": "Продукт", "cover_file_id": None}, "cover_file_id")
    await forms.put(100, draft)
    m = msg()
    m.photo = [SimpleNamespace(file_id="telegram-photo")]
    await message(m, runtime)
    draft = await forms.load(100)
    assert draft["data"]["cover_file_id"] == "telegram-photo"
    assert draft["step"] == "confirm"


async def test_channel_check_failure_keeps_draft_and_hides_raw_error(runtime):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetChat

    forms = AdminForms(runtime)
    draft = new_draft("channel")
    draft["step"] = "confirm"
    draft["data"].update(telegram_chat_id=-1001234567890, title="Канал")
    await forms.put(100, draft)
    runtime.admin.save.side_effect = TelegramBadRequest(
        method=GetChat(chat_id=-1001234567890), message="RAW_TELEGRAM_INTERNAL_DETAILS"
    )
    with pytest.raises(Denied, match="проверить канал") as error:
        await callback(query(f"aw:{draft['token']}:save"), runtime)
    assert "RAW_TELEGRAM_INTERNAL_DETAILS" not in str(error.value)
    assert await forms.load(100) is not None
