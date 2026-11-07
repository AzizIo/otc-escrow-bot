from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from app.config import settings
from app.db import SessionFactory
from app.models import Account, User

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


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Пока доступен просмотр внутреннего баланса. Создание сделок и админ-панель "
        "будут добавлены следующим этапом. Для поддержки обратись к администратору."
    )


def create_bot() -> Bot:
    return Bot(token=settings.bot_token)
