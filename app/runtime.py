from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from redis.asyncio import Redis
from app.core.logging import configure
from app.core.security import RateLimiter
from app.db.session import Database
from app.services.catalog import CatalogService
from app.services.orders import OrderService
from app.services.payments import PaymentService
from app.services.telegram_access import AccessService
from app.services.subscriptions import SubscriptionService
from app.services.refunds import RefundService
from app.services.admin import AdminService
from app.services.wallet import WalletService


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        configure(settings)
        self.db = Database(settings.database_url.get_secret_value())
        self.redis = Redis.from_url(
            settings.redis_url.get_secret_value(), socket_timeout=3, socket_connect_timeout=3
        )
        self.bot = Bot(settings.bot_token.get_secret_value(), session=AiohttpSession(timeout=6))
        self.limiter = RateLimiter(self.redis)
        self.catalog = CatalogService(self.db)
        self.wallet = WalletService(self.db, settings)
        self.orders = OrderService(self.db, settings)
        self.payments = PaymentService(self.db)
        self.access = AccessService(self.db, self.bot, settings)
        self.subscriptions = SubscriptionService(self.db, self.bot)
        self.refunds = RefundService(self.db, self.bot, settings)
        self.admin = AdminService(self.db, self.access, settings)

    async def close(self):
        await self.bot.session.close()
        await self.redis.aclose()
        await self.db.engine.dispose()
        await self.db.lock_engine.dispose()
