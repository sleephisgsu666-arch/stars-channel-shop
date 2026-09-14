import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError
from aiogram.methods import AnswerCallbackQuery, GetUpdates
from aiogram.types import Update
from app.bot.callbacks import answer_callback
from app.cli import poll


async def test_expired_callback_ack_does_not_abort_handler():
    query = SimpleNamespace(
        answer=AsyncMock(
            side_effect=TelegramBadRequest(
                method=AnswerCallbackQuery(callback_query_id="old"),
                message="Bad Request: query is too old and response timeout expired or query ID is invalid",
            )
        )
    )
    await answer_callback(query)
    query.answer.assert_awaited_once()


async def test_other_callback_errors_are_not_swallowed():
    query = SimpleNamespace(
        answer=AsyncMock(
            side_effect=TelegramBadRequest(
                method=AnswerCallbackQuery(callback_query_id="bad"), message="unrelated error"
            )
        )
    )
    with pytest.raises(TelegramBadRequest):
        await answer_callback(query)


async def test_failed_update_is_retried_before_offset_advances(monkeypatch):
    import app.cli as cli

    batch = [Update(update_id=10), Update(update_id=11)]
    bot = SimpleNamespace(get_updates=AsyncMock(side_effect=[batch, [batch[1]], asyncio.CancelledError()]))
    dispatch = AsyncMock(side_effect=[None, RuntimeError("database unavailable"), None])
    sleep = AsyncMock()
    monkeypatch.setattr(cli, "dispatch_update", dispatch)
    monkeypatch.setattr(cli.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await poll(SimpleNamespace(bot=bot), object())
    assert [c.kwargs["offset"] for c in bot.get_updates.call_args_list] == [None, 11, 12]
    assert [c.args[2]["update_id"] for c in dispatch.call_args_list] == [10, 11, 11]
    sleep.assert_awaited_once_with(1)


async def test_network_failure_keeps_polling(monkeypatch):
    import app.cli as cli

    bot = SimpleNamespace(get_updates=AsyncMock(side_effect=[TimeoutError(), [], asyncio.CancelledError()]))
    monkeypatch.setattr(cli.asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await poll(SimpleNamespace(bot=bot), object())
    assert bot.get_updates.await_count == 3


async def test_second_polling_instance_is_reported():
    bot = SimpleNamespace(
        get_updates=AsyncMock(
            side_effect=TelegramConflictError(method=GetUpdates(), message="another getUpdates instance")
        )
    )
    with pytest.raises(TelegramConflictError):
        await poll(SimpleNamespace(bot=bot), object())


async def test_admin_help_still_opens_after_expired_callback():
    from app.bot.handlers.admin import callback

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        data="adminhelp",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(
            side_effect=TelegramBadRequest(
                method=AnswerCallbackQuery(callback_query_id="old"), message="query is too old"
            )
        ),
    )
    runtime = SimpleNamespace(
        admin=SimpleNamespace(auth=lambda uid: None), limiter=SimpleNamespace(check=AsyncMock())
    )
    await callback(query, runtime)
    query.message.answer.assert_awaited_once()
