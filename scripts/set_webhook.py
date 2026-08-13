"""One-off script: registers this deployment's webhook URL with Telegram.

Usage:
    python scripts/set_webhook.py https://your-app.vercel.app
"""

import asyncio
import sys

from aiogram import Bot

sys.path.insert(0, ".")

from bot_api.config import get_settings  # noqa: E402


async def main(base_url: str) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    webhook_url = f"{base_url.rstrip('/')}/telegram/webhook/{settings.telegram_webhook_secret}"
    bot = Bot(token=settings.telegram_bot_token)
    try:
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        info = await bot.get_webhook_info()
        print(f"Webhook set to: {webhook_url}")
        print(info)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/set_webhook.py https://your-app.vercel.app")
    asyncio.run(main(sys.argv[1]))
