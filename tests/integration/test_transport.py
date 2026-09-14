from datetime import datetime, timezone
from types import SimpleNamespace
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Chat, User as TelegramUser, ChatInviteLink
from app.bot.dispatcher import build_dispatcher
from app.api.webhook import dispatch_update
from app.services.orders import OrderService
from app.services.payments import PaymentService
from app.services.catalog import CatalogService
from app.services.telegram_access import AccessService
from app.services.subscriptions import SubscriptionService
from app.services.refunds import RefundService
from unittest.mock import AsyncMock
from sqlalchemy import select, func
from app.db.models import Payment, Entitlement, Order


class TelegramTransport(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name == "createInvoiceLink":
            return "https://t.me/$testinvoice"
        if name == "createChatInviteLink":
            return ChatInviteLink(
                invite_link="https://t.me/+owner-only",
                creator=TelegramUser(id=999, is_bot=True, first_name="Bot"),
                creates_join_request=True,
                is_primary=False,
                is_revoked=False,
            )
        if name.startswith("send"):
            return Message(
                message_id=900, date=datetime.now(timezone.utc), chat=Chat(id=101, type="private"), text="OK"
            )
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


def callback_payload(update_id, data):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": 101, "is_bot": False, "first_name": "Buyer"},
            "chat_instance": "private",
            "data": data,
            "message": {"message_id": 5, "date": 1700000000, "chat": {"id": 101, "type": "private"}},
        },
    }


async def test_wallet_end_to_end_transport(db, settings, seeded):
    from app.services.wallet import WalletService

    session = TelegramTransport()
    bot = Bot(settings.bot_token.get_secret_value(), session=session)
    runtime = SimpleNamespace(
        db=db,
        bot=bot,
        settings=settings,
        redis=SimpleNamespace(get=AsyncMock(return_value=None), delete=AsyncMock()),
        limiter=SimpleNamespace(check=AsyncMock()),
        catalog=CatalogService(db),
        orders=OrderService(db, settings),
        payments=PaymentService(db),
        wallet=WalletService(db, settings),
        access=AccessService(db, bot, settings),
        subscriptions=SubscriptionService(db, bot),
        refunds=RefundService(db, bot, settings),
    )
    dp = build_dispatcher(runtime)
    await dispatch_update(runtime, dp, callback_payload(1, "w:invoice:500"))
    invoice = next(m for m in session.calls if m.__api_method__ == "sendInvoice")
    assert invoice.currency == "XTR" and invoice.prices[0].amount == 500
    await dispatch_update(
        runtime,
        dp,
        {
            "update_id": 2,
            "pre_checkout_query": {
                "id": "checkout",
                "from": {"id": 101, "is_bot": False, "first_name": "Buyer"},
                "currency": "XTR",
                "total_amount": 500,
                "invoice_payload": invoice.payload,
            },
        },
    )
    payload = {
        "update_id": 3,
        "message": {
            "message_id": 3,
            "date": 1700000000,
            "chat": {"id": 101, "type": "private"},
            "from": {"id": 101, "is_bot": False, "first_name": "Buyer"},
            "successful_payment": {
                "currency": "XTR",
                "total_amount": 500,
                "invoice_payload": invoice.payload,
                "telegram_payment_charge_id": "topup-event",
                "provider_payment_charge_id": "",
            },
        },
    }
    await dispatch_update(runtime, dp, payload)
    await dispatch_update(runtime, dp, payload)
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 0
    await dispatch_update(runtime, dp, callback_payload(4, f"plan:{seeded.plans['fixed'].id}"))
    async with db.sessions() as s:
        order = await s.scalar(select(Order).where(Order.funding_source == "wallet"))
    await dispatch_update(runtime, dp, callback_payload(5, f"spend:{order.id}"))
    assert await runtime.wallet.balance(101) == 250
    await dispatch_update(runtime, dp, callback_payload(6, f"access:{seeded.product.id}"))
    await dispatch_update(
        runtime,
        dp,
        {
            "update_id": 7,
            "chat_join_request": {
                "chat": {"id": seeded.channel.telegram_chat_id, "type": "channel", "title": "Private"},
                "from": {"id": 101, "is_bot": False, "first_name": "Buyer"},
                "user_chat_id": 101,
                "date": 1700000000,
                "invite_link": {
                    "invite_link": "https://t.me/+owner-only",
                    "creator": {"id": 999, "is_bot": True, "first_name": "Bot"},
                    "creates_join_request": True,
                    "is_primary": False,
                    "is_revoked": False,
                },
            },
        },
    )
    assert any(m.__api_method__ == "approveChatJoinRequest" for m in session.calls)
    assert not any(m.__api_method__ == "createInvoiceLink" for m in session.calls)
    assert sum(m.__api_method__ == "sendInvoice" for m in session.calls) == 1
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Payment)) == 1
    await bot.session.close()
