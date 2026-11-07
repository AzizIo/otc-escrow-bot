import asyncio
import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.bot import create_bot, dp
from app.config import settings
from app.db import engine


async def configure_commands(bot: Bot) -> None:
    user_commands = [
        BotCommand(command="start", description="Регистрация и главное меню"),
        BotCommand(command="balance", description="Мой внутренний баланс"),
        BotCommand(command="deal", description="Создать сделку"),
        BotCommand(command="my_deals", description="Мои последние сделки"),
        BotCommand(command="help", description="Как пользоваться ботом"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    admin_commands = user_commands + [
        BotCommand(command="admin", description="Панель администратора"),
        BotCommand(command="credit", description="Начислить внутренний баланс"),
        BotCommand(command="debit", description="Списать внутренний баланс"),
        BotCommand(command="resolve_release", description="Выплатить продавцу по сделке"),
        BotCommand(command="resolve_refund", description="Вернуть покупателю по сделке"),
    ]
    await bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(chat_id=settings.superadmin_id),
    )


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    bot = create_bot()
    try:
        await configure_commands(bot)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
