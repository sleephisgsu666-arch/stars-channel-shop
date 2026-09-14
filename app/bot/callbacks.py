"""Acknowledging a button is best-effort when Telegram's query has expired."""

from aiogram.exceptions import TelegramBadRequest
from app.core.logging import event


async def answer_callback(query) -> None:
    try:
        await query.answer()
    except TelegramBadRequest as error:
        message = error.message.lower()
        if (
            "query is too old" not in message
            and "query_id_invalid" not in message
            and "query id is invalid" not in message
        ):
            raise
        # Only the acknowledgement has expired. Domain checks and idempotency still run.
        event("callback_ack_expired")
