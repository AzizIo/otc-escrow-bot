from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserRole(StrEnum):
    USER = "user"
    SUPERADMIN = "superadmin"


class DealStatus(StrEnum):
    CREATED = "created"
    AWAITING_SELLER = "awaiting_seller"
    AWAITING_FUNDING = "awaiting_funding"
    FUNDED = "funded"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    REFUNDED = "refunded"


class LedgerKind(StrEnum):
    ADMIN_CREDIT = "admin_credit"
    ADMIN_DEBIT = "admin_debit"
    DEPOSIT_CREDIT = "deposit_credit"
    DEAL_FREEZE = "deal_freeze"
    DEAL_RELEASE = "deal_release"
    DEAL_REFUND = "deal_refund"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(160))
    is_blocked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    profile_deals_override: Mapped[int | None] = mapped_column()
    profile_successful_override: Mapped[int | None] = mapped_column()
    profile_since_override: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("available >= 0", name="ck_accounts_available_nonnegative"),
        CheckConstraint("frozen >= 0", name="ck_accounts_frozen_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    available: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    frozen: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(8), default="USDT")


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_deals_amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=DealStatus.CREATED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    available_delta: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    frozen_delta: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CryptoDeposit(Base):
    __tablename__ = "crypto_deposits"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_crypto_deposits_amount_positive"),
    )

    invoice_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )



class UserAdminAudit(Base):
    __tablename__ = "user_admin_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    field: Mapped[str] = mapped_column(String(48))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
