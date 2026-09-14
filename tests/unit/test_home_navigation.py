from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.handlers.shop import start
from app.bot.keyboards import keyboard, invoice_keyboard


class Lock:
    async def __aenter__(self):
        pass

    async def __aexit__(self, *args):
        pass


async def test_home_uses_clicking_user_and_cancels_admin_draft(monkeypatch):
    user = SimpleNamespace(id=100)
    ensure = AsyncMock(return_value=SimpleNamespace(is_blocked=False))
    monkeypatch.setattr("app.services.catalog.ensure_user", ensure)
    r = SimpleNamespace(
        settings=SimpleNamespace(admin_telegram_ids=[100]),
        admin=SimpleNamespace(auth=lambda uid: None),
        redis=SimpleNamespace(delete=AsyncMock()),
        db=SimpleNamespace(lock=lambda key: Lock()),
    )
    message = SimpleNamespace(from_user=SimpleNamespace(id=999), answer=AsyncMock())
    await start(message, r, user)
    ensure.assert_awaited_once_with(r.db, user)
    r.redis.delete.assert_any_await("admin-form:100")
    buttons = [
        b.callback_data
        for row in message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "admin" in buttons and "catalog:0" in buttons


async def test_start_command_also_exits_admin_form(monkeypatch):
    monkeypatch.setattr(
        "app.services.catalog.ensure_user", AsyncMock(return_value=SimpleNamespace(is_blocked=False))
    )
    r = SimpleNamespace(
        settings=SimpleNamespace(admin_telegram_ids=[100]),
        admin=SimpleNamespace(auth=lambda uid: None),
        redis=SimpleNamespace(delete=AsyncMock()),
        db=SimpleNamespace(lock=lambda key: Lock()),
    )
    message = SimpleNamespace(from_user=SimpleNamespace(id=100), answer=AsyncMock())
    await start(message, r)
    r.redis.delete.assert_any_await("admin-form:100")


def test_invoice_home_keeps_pay_button_first_and_no_duplicate_navigation():
    markup = invoice_keyboard(250)
    assert markup.inline_keyboard[0][0].pay is True
    assert markup.inline_keyboard[1][0].callback_data == "home"
    rows = [[("В меню", "home")]]
    assert len(keyboard(rows).inline_keyboard) == 1
    assert rows == [[("В меню", "home")]]
