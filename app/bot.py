from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from app.config import settings
from app.db import SessionFactory
from app.models import Account, User
from app.services.ledger import admin_adjust

dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message) -> None:
    async with SessionFactory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user is None:
            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
            session.add(user)
            await session.flush()
            session.add(Account(user_id=user.id, currency=settings.currency))
        else:
            user.username = message.from_user.username
            user.full_name = message.from_user.full_name

    await message.answer(
        "Добро пожаловать в OTC Escrow.\n\n"
        "Баланс здесь — только внутренняя учётная запись в условных USDT, "
        "не реальные токены и не криптокошелёк.\n\n"
        "Команды: /balance — баланс, /help — помощь."
    )


@dp.message(Command("balance"))
async def balance(message: Message) -> None:
    async with SessionFactory() as session:
        account = await session.scalar(
            select(Account)
            .join(User, User.id == Account.user_id)
            .where(User.telegram_id == message.from_user.id)
        )
        if account is None:
            await message.answer("Сначала отправь /start.")
            return
        await message.answer(
            f"Доступно: {account.available:.2f} {account.currency}\n"
            f"Заморожено: {account.frozen:.2f} {account.currency}"
        )


async def _adjust_balance(message: Message, *, credit: bool) -> None:
    if message.from_user.id != settings.superadmin_id:
        await message.answer("⛔ Эта команда доступна только администратору.")
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) != 4:
        action = "credit" if credit else "debit"
        await message.answer(
            f"Формат: /{action} TELEGRAM_ID СУММА ПРИЧИНА\n"
            f"Пример: /{action} 123456789 25.00 Оплата по заявке"
        )
        return

    try:
        target_id = int(parts[1])
        amount = Decimal(parts[2])
        if target_id <= 0 or not amount.is_finite() or amount <= 0:
            raise ValueError
        async with SessionFactory() as session, session.begin():
            await admin_adjust(
                session,
                target_telegram_id=target_id,
                actor_telegram_id=message.from_user.id,
                amount=amount,
                credit=credit,
                reason=parts[3],
            )
    except (ValueError, InvalidOperation) as exc:
        await message.answer(f"Не удалось выполнить операцию: {exc or 'проверь ID и сумму'}.")
        return

    verb = "начислено" if credit else "списано"
    await message.answer(f"✅ {verb.capitalize()} {amount:.2f} USDT пользователю {target_id}.")


@dp.message(Command("credit"))
async def credit(message: Message) -> None:
    await _adjust_balance(message, credit=True)


@dp.message(Command("debit"))
async def debit(message: Message) -> None:
    await _adjust_balance(message, credit=False)


@dp.message(Command("admin"))
async def admin_help(message: Message) -> None:
    if message.from_user.id != settings.superadmin_id:
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer(
        "Панель администратора (команды):\n"
        "/credit ID СУММА ПРИЧИНА — начислить внутренний баланс\n"
        "/debit ID СУММА ПРИЧИНА — списать доступный баланс\n\n"
        "Каждая операция записывается в журнал."
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Доступно:\n/balance — внутренний баланс\n"
        "/help — помощь\n\n"
        "Сделки и споры будут добавлены следующим этапом. "
        "Для поддержки обратись к администратору."
    )


def create_bot() -> Bot:
    return Bot(token=settings.bot_token)
