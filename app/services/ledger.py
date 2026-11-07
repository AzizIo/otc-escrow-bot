from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, LedgerEntry, User


CENT = Decimal("0.01")


def money(value: Decimal | str | int) -> Decimal:
    amount = Decimal(str(value)).quantize(CENT)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    return amount


async def admin_adjust(
    session: AsyncSession,
    *,
    target_telegram_id: int,
    actor_telegram_id: int,
    amount: Decimal,
    credit: bool,
    reason: str,
) -> None:
    amount = money(amount)
    if not reason.strip():
        raise ValueError("A reason is required")

    user = await session.scalar(select(User).where(User.telegram_id == target_telegram_id))
    if user is None:
        user = User(telegram_id=target_telegram_id)
        session.add(user)
        await session.flush()

    account = await session.scalar(
        select(Account).where(Account.user_id == user.id).with_for_update()
    )
    if account is None:
        account = Account(user_id=user.id, currency="USDT")
        session.add(account)
        await session.flush()

    if not credit and account.available < amount:
        raise ValueError("Insufficient available balance")

    delta = amount if credit else -amount
    account.available += delta
    session.add(
        LedgerEntry(
            account_id=account.id,
            kind="admin_credit" if credit else "admin_debit",
            amount=amount,
            available_delta=delta,
            frozen_delta=Decimal("0.00"),
            actor_telegram_id=actor_telegram_id,
            reason=reason.strip(),
        )
    )
