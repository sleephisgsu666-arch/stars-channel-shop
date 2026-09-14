import argparse
import asyncio
import socket
from app.core.config import Settings
from app.runtime import Runtime
from app.bot.dispatcher import build_dispatcher
from app.api.webhook import dispatch_update
from app.core.logging import event
from aiogram.exceptions import TelegramUnauthorizedError, TelegramConflictError, TelegramRetryAfter

ALLOWED = ["message", "callback_query", "pre_checkout_query", "chat_join_request", "subscription"]


async def poll(runtime, dispatcher):
    offset = None
    delay = 1
    while True:
        try:
            updates = await runtime.bot.get_updates(
                offset=offset, timeout=5, allowed_updates=ALLOWED, request_timeout=15
            )
            for update in updates:
                await dispatch_update(runtime, dispatcher, update.model_dump(mode="json", exclude_none=True))
                # Advance only after processing succeeds. Failed payments must be delivered again.
                offset = update.update_id + 1
            delay = 1
        except (TelegramUnauthorizedError, TelegramConflictError):
            raise
        except Exception as error:
            wait = max(delay, error.retry_after) if isinstance(error, TelegramRetryAfter) else delay
            event("polling_retry", error_type=type(error).__name__, retry_seconds=wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30)


async def run(command):
    r = Runtime(Settings())
    try:
        if command == "set-webhook":
            await r.bot.set_webhook(
                r.settings.webhook_base_url.rstrip("/") + "/telegram/webhook/" + r.settings.webhook_path,
                secret_token=r.settings.webhook_secret.get_secret_value(),
                allowed_updates=ALLOWED,
                drop_pending_updates=False,
            )
            print("Webhook configured.")
        elif command == "poll":
            if r.settings.app_env != "development":
                raise RuntimeError("Polling is allowed only in development")
            await r.bot.delete_webhook(drop_pending_updates=False)
            dp = build_dispatcher(r)
            await poll(r, dp)
        elif command == "worker-health":
            if not await r.redis.exists(f"worker:heartbeat:{socket.gethostname()}"):
                raise RuntimeError("No worker heartbeat")
    finally:
        await r.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["set-webhook", "poll", "worker-health"])
    asyncio.run(run(parser.parse_args().command))
