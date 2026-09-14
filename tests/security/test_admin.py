import json
from sqlalchemy import select, func
import pytest
from app.core.security import Denied
from app.services.admin import AdminService
from app.services.telegram_access import AccessService
from app.db.models import AuditLog, Product, Entitlement, Grant


async def test_admin_impersonation(db, settings, seeded, bot):
    admin = AdminService(db, AccessService(db, bot, settings), settings)
    with pytest.raises(Denied):
        await admin.stats(101)
    with pytest.raises(Denied):
        await admin.save(101, "product", None, "{}", "malicious")
    assert (await admin.stats(100))["users"] == 2


async def test_admin_manual_idempotency_and_audit(db, settings, seeded, bot):
    admin = AdminService(db, AccessService(db, bot, settings), settings)
    data = json.dumps(dict(telegram_user_id=101, product_id=str(seeded.product.id), days=30))
    a = await admin.manual(100, data, "once")
    b = await admin.manual(100, data, "once")
    assert a == b
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(AuditLog)) == 1
        assert await s.scalar(select(func.count()).select_from(Grant)) == 1
        assert await s.scalar(select(func.count()).select_from(Entitlement)) == 1


async def test_sql_injection_is_literal(db, settings, seeded, bot):
    admin = AdminService(db, AccessService(db, bot, settings), settings)
    title = "'; DROP TABLE users; --"
    payload = json.dumps(dict(channel_id=str(seeded.channel.id), title=title, is_active=True))
    await admin.save(100, "product", seeded.product.id, payload, "sql")
    async with db.sessions() as s:
        assert (await s.get(Product, seeded.product.id)).title == title
    assert (await admin.stats(100))["users"] == 2
