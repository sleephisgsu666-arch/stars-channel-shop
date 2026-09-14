import asyncio
import time
import socket
from sqlalchemy import select, or_
from app.db.models import Entitlement, InviteLink, Order, RefundRequest, now
from app.core.logging import event


class Jobs:
    def __init__(self, runtime):
        self.r = runtime
        self.heartbeat = 0.0

    async def scan(self, model, *conditions):
        cursor = None
        while True:
            async with self.r.db.sessions() as session:
                query = select(model).where(*conditions)
                if cursor is not None:
                    query = query.where(model.id > cursor)
                rows = list(await session.scalars(query.order_by(model.id).limit(200)))
            if not rows:
                return
            for row in rows:
                yield row
            cursor = rows[-1].id

    async def expire_entitlements(self):
        async for ent in self.scan(
            Entitlement,
            or_(
                Entitlement.removal_pending.is_(True),
                (Entitlement.status == "active") & (Entitlement.expires_at <= now()),
            ),
        ):
            try:
                await self.r.access.remove(ent.id)
            except Exception as exc:
                event(
                    "telegram_api_error", operation="remove", entity_id=ent.id, error_type=type(exc).__name__
                )

    async def cleanup_invites(self):
        async for link in self.scan(
            InviteLink,
            InviteLink.revoked_at.is_(None),
            or_(InviteLink.expires_at <= now(), InviteLink.used_at.is_not(None)),
        ):
            try:
                await self.r.access.revoke(link)
            except Exception as exc:
                event("telegram_api_error", operation="revoke", error_type=type(exc).__name__)

    async def refunds(self):
        async for request in self.scan(RefundRequest, RefundRequest.status == "pending"):
            try:
                await self.r.refunds.process(request.payment_id)
            except Exception as exc:
                event(
                    "telegram_api_error",
                    operation="refund",
                    payment_id=request.payment_id,
                    error_type=type(exc).__name__,
                )

    async def reconciliation(self):
        async for ent in self.scan(Entitlement, Entitlement.status != "active"):
            try:
                await self.r.access.remove(ent.id)
            except Exception as exc:
                event(
                    "telegram_api_error",
                    operation="reconcile",
                    entity_id=ent.id,
                    error_type=type(exc).__name__,
                )
        while True:
            async with self.r.db.transaction() as session:
                orders = list(
                    await session.scalars(
                        select(Order)
                        .where(
                            Order.status.in_(["pending", "precheckout_approved"]), Order.expires_at <= now()
                        )
                        .with_for_update(skip_locked=True)
                        .limit(1000)
                    )
                )
                for order in orders:
                    order.status = "expired"
            if len(orders) < 1000:
                break
        await self.cleanup_invites()

    async def cancel_legacy_subscriptions(self):
        from app.db.models import User

        async for order in self.scan(
            Order,
            Order.billing_type == "recurring_30d",
            Order.subscription_charge_id.is_not(None),
            or_(Order.subscription_state.is_(None), Order.subscription_state != "canceled"),
        ):
            try:
                async with self.r.db.sessions() as session:
                    user = await session.get(User, order.user_id)
                await self.r.subscriptions.change(user.telegram_user_id, order.id, True)
            except Exception as exc:
                event(
                    "telegram_api_error",
                    operation="cancel_legacy_subscription",
                    order_id=order.id,
                    error_type=type(exc).__name__,
                )

    async def run(self):
        schedules = [
            ("expire_entitlements", self.r.settings.expire_interval),
            ("cleanup_invites", self.r.settings.cleanup_interval),
            ("reconciliation", self.r.settings.reconciliation_interval),
            ("refunds", 15),
            ("cancel_legacy_subscriptions", 60),
        ]

        async def periodic(name, interval):
            while True:
                try:
                    # Global per-job mutex; entity locks coordinate with web processes.
                    async with self.r.db.lock(f"job:{name}"):
                        await getattr(self, name)()
                except Exception as exc:
                    event("worker_error", job=name, error_type=type(exc).__name__)
                await asyncio.sleep(interval)

        async def heartbeat():
            while True:
                await self.r.redis.set(f"worker:heartbeat:{socket.gethostname()}", str(time.time()), ex=120)
                await asyncio.sleep(15)

        await asyncio.gather(*(periodic(n, i) for n, i in schedules), heartbeat())


async def main():
    from app.runtime import Runtime
    from app.core.config import Settings

    runtime = Runtime(Settings())
    try:
        await Jobs(runtime).run()
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
