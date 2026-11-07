import asyncio
import logging

from app.bot import create_bot, dp
from app.config import settings
from app.db import engine


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    bot = create_bot()
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
