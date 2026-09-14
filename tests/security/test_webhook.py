from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from app.api.webhook import router, dispatch_update
from app.db.models import UpdateReceipt


@pytest.mark.parametrize("secret,status", [(None, 403), ("wrong", 403), ("s" * 40, 200)])
async def test_webhook_secret(settings, monkeypatch, secret, status):
    app = FastAPI()
    app.include_router(router(settings))
    app.state.runtime, app.state.dispatcher = object(), object()
    process = AsyncMock()
    monkeypatch.setattr("app.api.webhook.dispatch_update", process)
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook/" + settings.webhook_path, json={"update_id": 100}, headers=headers
        )
    assert response.status_code == status
    assert process.await_count == int(status == 200)


async def test_duplicate_update(db, bot):
    runtime = SimpleNamespace(db=db, bot=bot)
    dispatcher = SimpleNamespace(feed_update=AsyncMock())
    await dispatch_update(runtime, dispatcher, {"update_id": 123})
    await dispatch_update(runtime, dispatcher, {"update_id": 123})
    dispatcher.feed_update.assert_awaited_once()


async def test_failure_retries_update(db, bot):
    runtime = SimpleNamespace(db=db, bot=bot)
    dispatcher = SimpleNamespace(feed_update=AsyncMock(side_effect=[RuntimeError(), None]))
    with pytest.raises(RuntimeError):
        await dispatch_update(runtime, dispatcher, {"update_id": 123})
    async with db.sessions() as s:
        assert await s.get(UpdateReceipt, 123) is None
    await dispatch_update(runtime, dispatcher, {"update_id": 123})
    assert dispatcher.feed_update.await_count == 2


async def test_oversized_webhook(settings):
    app = FastAPI()
    app.include_router(router(settings))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook/" + settings.webhook_path,
            content=b"x" * 262145,
            headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 40},
        )
    assert response.status_code == 413
