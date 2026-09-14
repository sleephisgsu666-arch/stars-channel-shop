import asyncio
from aiogram.types import Update, User as TelegramUser
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal
from app.core.security import secret_matches, Denied
from app.core.logging import event
from app.db.models import UpdateReceipt
from app.bot.keyboards import keyboard


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user: TelegramUser
    invoice_payload: str = Field(max_length=128)
    state: Literal["active", "canceled", "failed"]


async def dispatch_update(runtime, dispatcher, payload):
    update = Update.model_validate(payload, context={"bot": runtime.bot})
    async with runtime.db.lock(f"update:{update.update_id}"):
        async with runtime.db.sessions() as s:
            if await s.get(UpdateReceipt, update.update_id):
                return
        try:
            # Compatibility adapter for Bot API 10.2's subscription field, even on older aiogram.
            if "subscription" in payload:
                subscription = SubscriptionUpdate.model_validate(payload["subscription"])
                await runtime.subscriptions.updated(
                    subscription.user.id, subscription.invoice_payload, subscription.state
                )
            else:
                await dispatcher.feed_update(runtime.bot, update)
        except (Denied, ValueError, ValidationError) as exc:
            event("update_rejected", update_id=update.update_id, error_type=type(exc).__name__)
            message = update.message or (update.callback_query.message if update.callback_query else None)
            if message and message.chat.type == "private":
                await runtime.bot.send_message(
                    message.chat.id,
                    str(exc) if isinstance(exc, Denied) else "Некорректные данные. Проверьте формат команды.",
                    reply_markup=keyboard([]),
                )
        async with runtime.db.transaction() as s:
            s.add(UpdateReceipt(update_id=update.update_id))


def router(settings):
    api = APIRouter()

    @api.post("/telegram/webhook/" + settings.webhook_path)
    async def webhook(request: Request):
        if not secret_matches(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
            settings.webhook_secret.get_secret_value(),
        ):
            raise HTTPException(403, "Forbidden")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, "Request too large")
        import json

        try:
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or type(payload.get("update_id")) is not int
                or payload["update_id"] < 0
            ):
                raise ValueError()
            # Bound the synchronous path. Telegram retries on failure; receipt is committed last.
            async with asyncio.timeout(8 if "pre_checkout_query" in payload else 50):
                await dispatch_update(request.app.state.runtime, request.app.state.dispatcher, payload)
        except (ValueError, ValidationError):
            raise HTTPException(400, "Invalid update") from None
        except Exception as exc:
            event("webhook_error", error_type=type(exc).__name__)
            raise HTTPException(503, "Temporarily unavailable") from None
        return {"ok": True}

    return api
