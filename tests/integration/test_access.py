from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.db.models import Entitlement, InviteLink, now
from app.services.orders import OrderService
from app.services.telegram_access import AccessService
from app.core.security import Denied


async def prepare(db, settings, seeded, bot):
    ent = await OrderService(db, settings).free(101, seeded.plans["free"].id)
    service = AccessService(db, bot, settings)
    link = await service.invite(101, seeded.product.id)
    request = SimpleNamespace(
        invite_link=SimpleNamespace(invite_link=link),
        from_user=SimpleNamespace(id=101),
        chat=SimpleNamespace(id=seeded.channel.telegram_chat_id),
    )
    return service, ent, request


async def test_owner_approve(db, settings, seeded, bot):
    service, ent, request = await prepare(db, settings, seeded, bot)
    assert await service.join(request)
    bot.approve_chat_join_request.assert_awaited_once()
    bot.revoke_chat_invite_link.assert_awaited_once()
    async with db.sessions() as s:
        link = await s.scalar(select(InviteLink))
        assert link.used_at and link.revoked_at


async def test_shared_link_declines_then_owner_can_join(db, settings, seeded, bot):
    service, ent, request = await prepare(db, settings, seeded, bot)
    request.from_user.id = 202
    assert not await service.join(request)
    bot.approve_chat_join_request.assert_not_awaited()
    request.from_user.id = 101
    assert await service.join(request)


@pytest.mark.parametrize("invalid", ["link_expired", "ent_expired", "revoked", "wrong_channel"])
async def test_invalid_join(db, settings, seeded, bot, invalid):
    service, ent, request = await prepare(db, settings, seeded, bot)
    async with db.transaction() as s:
        if invalid == "link_expired":
            link = await s.scalar(select(InviteLink))
            link.expires_at = now() - timedelta(seconds=1)
        elif invalid == "ent_expired":
            stored = await s.get(Entitlement, ent.id)
            stored.expires_at = now() - timedelta(seconds=1)
        elif invalid == "revoked":
            stored = await s.get(Entitlement, ent.id)
            stored.status = "revoked"
        else:
            request.chat.id = -999
    assert not await service.join(request)
    bot.approve_chat_join_request.assert_not_awaited()


async def test_invite_idor(db, settings, seeded, bot):
    service, _, _ = await prepare(db, settings, seeded, bot)
    with pytest.raises(Denied):
        await service.invite(202, seeded.product.id)


async def test_expire_and_rejoin_recovery(db, settings, seeded, bot):
    service, ent, _ = await prepare(db, settings, seeded, bot)
    async with db.transaction() as s:
        stored = await s.get(Entitlement, ent.id)
        stored.expires_at = now() - timedelta(seconds=1)
    bot.unban_chat_member.reset_mock()
    await service.remove(ent.id)
    bot.ban_chat_member.assert_awaited_once()
    bot.unban_chat_member.assert_awaited_once_with(seeded.channel.telegram_chat_id, 101, only_if_banned=True)
    async with db.sessions() as s:
        stored = await s.get(Entitlement, ent.id)
        assert stored.status == "expired" and not stored.removal_pending


async def test_renewed_entitlement_not_removed(db, settings, seeded, bot):
    service, ent, _ = await prepare(db, settings, seeded, bot)
    await service.remove(ent.id)
    bot.ban_chat_member.assert_not_awaited()


async def test_ban_succeeds_unban_fails_recovered(db, settings, seeded, bot):
    service, ent, _ = await prepare(db, settings, seeded, bot)
    async with db.transaction() as s:
        stored = await s.get(Entitlement, ent.id)
        stored.expires_at = now() - timedelta(seconds=1)
    bot.unban_chat_member.side_effect = [RuntimeError("timeout"), True]
    with pytest.raises(RuntimeError):
        await service.remove(ent.id)
    async with db.sessions() as s:
        assert (await s.get(Entitlement, ent.id)).removal_pending
    await service.remove(ent.id)
    async with db.sessions() as s:
        assert not (await s.get(Entitlement, ent.id)).removal_pending


async def test_join_and_expiration_share_lock(db, settings, seeded, bot):
    import asyncio

    service, ent, request = await prepare(db, settings, seeded, bot)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_approve(*args, **kwargs):
        entered.set()
        await release.wait()
        return True

    bot.approve_chat_join_request.side_effect = slow_approve
    join = asyncio.create_task(service.join(request))
    await entered.wait()
    removal = asyncio.create_task(service.remove(ent.id))
    await asyncio.sleep(0.02)
    bot.ban_chat_member.assert_not_awaited()
    release.set()
    await asyncio.gather(join, removal)
    bot.ban_chat_member.assert_not_awaited()


async def test_renewal_waits_for_in_progress_removal(db, settings, seeded, bot):
    import asyncio
    from aiogram.types import SuccessfulPayment
    from app.services.payments import PaymentService

    service, ent, _ = await prepare(db, settings, seeded, bot)
    async with db.transaction() as s:
        stored = await s.get(Entitlement, ent.id)
        stored.expires_at = now() - timedelta(seconds=1)
    order = await OrderService(db, settings).create(101, seeded.plans["fixed"].id, "renewal-race")
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_ban(*args, **kwargs):
        entered.set()
        await release.wait()
        return True

    bot.ban_chat_member.side_effect = delayed_ban
    removal = asyncio.create_task(service.remove(ent.id))
    await entered.wait()
    renewal = asyncio.create_task(
        PaymentService(db).successful(
            101,
            SuccessfulPayment(
                currency="XTR",
                total_amount=250,
                invoice_payload=str(order.id),
                telegram_payment_charge_id="renewal-race",
                provider_payment_charge_id="",
            ),
        )
    )
    await asyncio.sleep(0.02)
    assert not renewal.done()
    release.set()
    await asyncio.gather(removal, renewal)
    async with db.sessions() as s:
        stored = await s.get(Entitlement, ent.id)
        assert stored.status == "active" and stored.expires_at > now() and not stored.removal_pending
