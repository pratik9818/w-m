"""Local-only entrypoint: runs the bot via long-polling instead of the Vercel webhook.

Lets us smoke-test the full onboarding flow in real Telegram before a deployment
exists. Not used in production — the deployed app uses bot_api/bot/webhook.py.
"""

import asyncio
import logging

from bot_api.bot.bot import get_bot, get_dispatcher
from bot_api.config import get_settings
from db.base import init_engine


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    init_engine(settings.database_url)

    bot = get_bot()
    dp = get_dispatcher()

    # Polling and a webhook can't both be active on the same bot token.
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
