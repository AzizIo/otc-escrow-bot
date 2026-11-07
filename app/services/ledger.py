from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, LedgerEntry, User


CENT = Decimal("0.01")


def money(value: Decimal | str | int) -> Decimal:
    amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
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

    await session.execute(
        pg_insert(User)
        .values(telegram_id=target_telegram_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_id])
    )
    user = await session.scalar(select(User).where(User.telegram_id == target_telegram_id))
    if user is None:
        raise RuntimeError("Could not create or load target user.")

    await session.execute(
        pg_insert(Account)
        .values(user_id=user.id, currency="USDT")
        .on_conflict_do_nothing(index_elements=[Account.user_id])
    )
    account = await session.scalar(
        select(Account).where(Account.user_id == user.id).with_for_update()
    )
    if account is None:
        raise RuntimeError("Could not create or lock target account.")

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
