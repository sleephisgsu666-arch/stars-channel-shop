from __future__ import annotations

from uuid import UUID
from aiogram import Bot
from aiogram.types import ChatJoinRequest
from app.db.session import Database
from app.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta
from sqlalchemy import select
from aiogram.exceptions import TelegramBadRequest
from app.core.security import Denied
from app.core.logging import event
from app.db.models import User, Product, Channel, Entitlement, InviteLink, now
from app.services.entitlements import active
from app.services.catalog import get_user


class AccessService:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db, self.bot, self.settings = db, bot, settings

    async def channel_check(self, chat_id: int) -> str:
        chat = await self.bot.get_chat(chat_id)
        me = await self.bot.get_me()
        member = await self.bot.get_chat_member(chat_id, me.id)
        if chat.type != "channel" or chat.username:
            raise Denied("Нужен закрытый канал без публичного username.")
        if member.status != "administrator" or not member.can_invite_users or not member.can_restrict_members:
            raise Denied("Боту нужны права приглашения и блокировки участников.")
        return chat.title

    async def context(
        self, s: AsyncSession, telegram_id: int, product_id: UUID
    ) -> tuple[Entitlement, Channel]:
        user = await get_user(s, telegram_id)
        product = await s.get(Product, product_id)
        channel = await s.get(Channel, product.channel_id) if product else None
        ent = await s.scalar(
            select(Entitlement).where(Entitlement.user_id == user.id, Entitlement.product_id == product_id)
        )
        # Hiding a product stops new sales, not access already purchased.
        if not channel or not channel.is_active or not active(ent):
            raise Denied("Нет действующего доступа.")
        return ent, channel

    async def invite(self, telegram_id: int, product_id: UUID) -> str:
        async with self.db.lock(f"access:{telegram_id}:{product_id}"):
            async with self.db.sessions() as s:
                ent, channel = await self.context(s, telegram_id, product_id)
                previous = await s.scalar(
                    select(InviteLink)
                    .where(
                        InviteLink.entitlement_id == ent.id,
                        InviteLink.revoked_at.is_(None),
                        InviteLink.used_at.is_(None),
                        InviteLink.expires_at > now(),
                    )
                    .order_by(InviteLink.created_at.desc())
                    .limit(1)
                )
                if previous:
                    return previous.invite_link
            await self.bot.unban_chat_member(channel.telegram_chat_id, telegram_id, only_if_banned=True)
            expires = (
                min(now() + timedelta(seconds=self.settings.invite_ttl), ent.expires_at)
                if ent.expires_at
                else now() + timedelta(seconds=self.settings.invite_ttl)
            )
            link = await self.bot.create_chat_invite_link(
                channel.telegram_chat_id, expire_date=expires, creates_join_request=True
            )
            try:
                async with self.db.transaction() as s:
                    s.add(
                        InviteLink(
                            entitlement_id=ent.id,
                            telegram_user_id=telegram_id,
                            telegram_chat_id=channel.telegram_chat_id,
                            invite_link=link.invite_link,
                            expires_at=expires,
                        )
                    )
            except BaseException:
                await self.bot.revoke_chat_invite_link(channel.telegram_chat_id, link.invite_link)
                raise
            event("invite_created", entitlement_id=ent.id)
            return link.invite_link

    async def join(self, request: ChatJoinRequest) -> bool:
        raw_link = request.invite_link.invite_link if request.invite_link else None
        async with self.db.sessions() as s:
            link = (
                await s.scalar(select(InviteLink).where(InviteLink.invite_link == raw_link))
                if raw_link
                else None
            )
            ent = await s.get(Entitlement, link.entitlement_id) if link else None
        if (
            not link
            or not ent
            or request.from_user.id != link.telegram_user_id
            or request.chat.id != link.telegram_chat_id
        ):
            await self.decline(request)
            return False
        async with self.db.lock(f"access:{request.from_user.id}:{ent.product_id}"):
            async with self.db.sessions() as s:
                link = await s.get(InviteLink, link.id)
                try:
                    current, channel = await self.context(s, request.from_user.id, ent.product_id)
                    valid = (
                        current.id == link.entitlement_id
                        and channel.telegram_chat_id == request.chat.id
                        and link.expires_at > now()
                        and not link.revoked_at
                        and not link.used_at
                    )
                except Denied:
                    valid = False
            if not valid:
                await self.decline(request)
                return False
            try:
                await self.bot.approve_chat_join_request(request.chat.id, request.from_user.id)
            except TelegramBadRequest:
                # A previous approve may have succeeded before its response was lost.
                member = await self.bot.get_chat_member(request.chat.id, request.from_user.id)
                if member.status not in ("member", "administrator", "creator"):
                    raise
            async with self.db.transaction() as s:
                stored = await s.get(InviteLink, link.id)
                stored.used_at = now()
            await self.revoke(link)
            event("join_approved", entitlement_id=ent.id)
            return True

    async def decline(self, request: ChatJoinRequest) -> None:
        try:
            await self.bot.decline_chat_join_request(request.chat.id, request.from_user.id)
        except TelegramBadRequest as exc:
            if "HIDE_REQUESTER_MISSING" not in exc.message:
                raise
        event("join_declined", telegram_user_id=request.from_user.id)

    async def revoke(self, link: InviteLink) -> None:
        try:
            await self.bot.revoke_chat_invite_link(link.telegram_chat_id, link.invite_link)
        except TelegramBadRequest as exc:
            if not any(code in exc.message for code in ("INVITE_HASH_EXPIRED", "INVITE_HASH_INVALID")):
                raise
        async with self.db.transaction() as s:
            stored = await s.get(InviteLink, link.id)
            stored.revoked_at = now()

    async def remove(self, ent_id: UUID) -> None:
        async with self.db.sessions() as s:
            ent = await s.get(Entitlement, ent_id)
            user = await s.get(User, ent.user_id)
        async with self.db.lock(f"access:{user.telegram_user_id}:{ent.product_id}"):
            async with self.db.transaction() as s:
                ent = await s.get(Entitlement, ent_id, with_for_update=True)
                if active(ent):
                    ent.removal_pending = False
                    return
                if ent.status == "active":
                    ent.status = "expired"
                ent.removal_pending = True
                product = await s.get(Product, ent.product_id)
                channel = await s.get(Channel, product.channel_id)
                links = list(
                    await s.scalars(
                        select(InviteLink).where(
                            InviteLink.entitlement_id == ent.id, InviteLink.revoked_at.is_(None)
                        )
                    )
                )
            # Retry both steps on uncertain response. The shared lock prevents a renewal/join race.
            await self.bot.ban_chat_member(channel.telegram_chat_id, user.telegram_user_id)
            await self.bot.unban_chat_member(
                channel.telegram_chat_id, user.telegram_user_id, only_if_banned=True
            )
            for link in links:
                await self.revoke(link)
            async with self.db.transaction() as s:
                stored = await s.get(Entitlement, ent_id)
                stored.removal_pending = False
            event("entitlement_expired", entitlement_id=ent_id)
