import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class User(Record, Base):
    __tablename__ = "users"
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(32))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    balance_stars: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        CheckConstraint("telegram_user_id > 0"),
        CheckConstraint("balance_stars >= 0", name="wallet_nonnegative"),
    )


class Channel(Record, Base):
    __tablename__ = "channels"
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Record, Base):
    __tablename__ = "products"
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channels.id"), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    short_description: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    cover_file_id: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Plan(Record, Base):
    __tablename__ = "plans"
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    billing_type: Mapped[str] = mapped_column(String(20))
    price_stars: Mapped[int] = mapped_column(Integer)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint(
            "(billing_type = 'free' AND price_stars = 0 AND "
            "(duration_days IS NULL OR duration_days > 0)) OR "
            "(billing_type = 'fixed' AND price_stars > 0 AND duration_days IS NOT NULL AND duration_days > 0) OR "
            "(billing_type = 'lifetime' AND price_stars > 0 AND duration_days IS NULL) OR "
            "(billing_type = 'recurring_30d' AND price_stars BETWEEN 1 AND 10000 AND duration_days IS NOT NULL AND duration_days = 30)",
            name="valid_plan",
        ),
        CheckConstraint("price_stars <= 1000000"),
    )


class Order(Record, Base):
    __tablename__ = "orders"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))
    funding_source: Mapped[str] = mapped_column(String(16), default="legacy", server_default="legacy")
    amount_stars: Mapped[int] = mapped_column(Integer)
    billing_type: Mapped[str] = mapped_column(String(20))
    duration_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    request_key: Mapped[str] = mapped_column(String(128), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    subscription_state: Mapped[str | None] = mapped_column(String(16))
    subscription_charge_id: Mapped[str | None] = mapped_column(String(256))
    subscription_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("amount_stars > 0"),
        CheckConstraint(
            "(funding_source = 'topup' AND product_id IS NULL AND plan_id IS NULL AND billing_type = 'topup') OR "
            "(funding_source IN ('legacy','wallet') AND product_id IS NOT NULL AND plan_id IS NOT NULL)",
            name="order_funding_shape",
        ),
        CheckConstraint(
            "status IN ('pending','precheckout_approved','paid','canceled','expired','refunded','failed')"
        ),
    )


class Payment(Record, Base):
    __tablename__ = "payments"
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(256), unique=True)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(256))
    amount_stars: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    is_first_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    subscription_expiration_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="paid")
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("currency = 'XTR' AND amount_stars > 0"),
        CheckConstraint("status IN ('paid','refunded')"),
    )


class Entitlement(Record, Base):
    __tablename__ = "entitlements"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    removal_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("user_id", "product_id"),
        CheckConstraint("status IN ('active','expired','revoked','refunded')"),
    )


class Grant(Record, Base):
    """Immutable grant ledger: refunds remove only the refunded contribution."""

    __tablename__ = "grants"
    entitlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entitlements.id"), index=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"), unique=True)
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    duration_days: Mapped[int | None] = mapped_column(Integer)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class InviteLink(Record, Base):
    __tablename__ = "invite_links"
    entitlement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entitlements.id"), index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    invite_link: Mapped[str] = mapped_column(String(512), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Record, Base):
    __tablename__ = "audit_logs"
    admin_telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(128))
    request_key: Mapped[str] = mapped_column(String(128), unique=True)
    old_data: Mapped[dict | None] = mapped_column(JSONB)
    new_data: Mapped[dict | None] = mapped_column(JSONB)


class UpdateReceipt(Base):
    __tablename__ = "update_receipts"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RefundRequest(Record, Base):
    __tablename__ = "refund_requests"
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), unique=True)
    admin_telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)


Index("orders_expiration_idx", Order.status, Order.expires_at)


class PaymentReversal(Record, Base):
    """A refund update may be delivered before its successful_payment update."""

    __tablename__ = "payment_reversals"
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(256), unique=True)
    amount_stars: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    __table_args__ = (CheckConstraint("currency = 'XTR' AND amount_stars > 0"),)


class WalletEntry(Record, Base):
    __tablename__ = "wallet_entries"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    __table_args__ = (CheckConstraint("amount <> 0"),)


class WalletAllocation(Record, Base):
    __tablename__ = "wallet_allocations"
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("purchase_order_id", "payment_id"), CheckConstraint("amount > 0"))
