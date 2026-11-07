import asyncio
import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.bot import create_bot, dp
from app.config import settings
from app.db import engine
from app.services.crypto_pay import poll_crypto_pay_invoices


async def configure_commands(bot: Bot) -> None:
    # All routine actions are available from inline buttons; commands remain as shortcuts.
    user_commands = [
        BotCommand(command="start", description="Запустить бота и открыть меню"),
        BotCommand(command="menu", description="Открыть главное меню"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    admin_commands = user_commands + [
        BotCommand(command="admin", description="Открыть панель администратора"),
    ]
    await bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(chat_id=settings.superadmin_id),
    )


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger(__name__).info("Starting OTC escrow bot")
    bot = create_bot()
    payment_task = asyncio.create_task(poll_crypto_pay_invoices(bot))
    try:
        await configure_commands(bot)
        await dp.start_polling(bot)
    except Exception:
        logging.getLogger(__name__).exception("Fatal error in bot polling")
        raise
    finally:
        payment_task.cancel()
        try:
            await payment_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await engine.dispose()
        logging.getLogger(__name__).info("Bot shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
