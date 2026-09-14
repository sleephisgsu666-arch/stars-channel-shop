import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.core.config import Settings
from app.db.session import Database
from app.db.models import Base, User, Channel, Product, Plan
from sqlalchemy import text


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        bot_token="123456789:" + "a" * 35,
        webhook_secret="s" * 40,
        webhook_path="p" * 40,
        webhook_base_url="https://example.com",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        redis_url="redis://localhost:6379/15",
        admin_telegram_ids=[100],
        support_contact="@support",
        terms_url="https://example.com/terms",
        app_env="test",
    )


@pytest.fixture
async def db():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database migrated to head")
    database = Database(url)
    async with database.engine.begin() as connection:
        # Fixed metadata-derived identifiers only, never user input. Test database is disposable.
        names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
        await connection.execute(text("TRUNCATE " + names + " CASCADE"))
    yield database
    await database.engine.dispose()
    await database.lock_engine.dispose()


@pytest.fixture
async def seeded(db):
    async with db.transaction() as s:
        user = User(telegram_user_id=101)
        other = User(telegram_user_id=202)
        channel = Channel(telegram_chat_id=-1001234567890, title="Private")
        s.add_all([user, other, channel])
        await s.flush()
        product = Product(channel_id=channel.id, title="Product", is_active=True)
        s.add(product)
        await s.flush()
        plans = {}
        for kind, price, days in [
            ("fixed", 250, 30),
            ("free", 0, 3),
            ("lifetime", 1500, None),
            ("recurring_30d", 200, 30),
        ]:
            plan = Plan(
                product_id=product.id, name=kind, billing_type=kind, price_stars=price, duration_days=days
            )
            s.add(plan)
            plans[kind] = plan
        await s.flush()
        return SimpleNamespace(user=user, other=other, channel=channel, product=product, plans=plans)


@pytest.fixture
def bot():
    b = AsyncMock()
    b.create_chat_invite_link.return_value = SimpleNamespace(invite_link="https://t.me/+private-test")
    b.get_chat.return_value = SimpleNamespace(type="channel", username=None, title="Private")
    b.get_me.return_value = SimpleNamespace(id=999)
    b.get_chat_member.return_value = SimpleNamespace(
        status="administrator", can_invite_users=True, can_restrict_members=True
    )
    return b
