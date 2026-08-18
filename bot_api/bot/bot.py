from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from bot_api.bot.handlers import edit, errors, manage, onboarding, sites, start
from bot_api.config import get_settings
from bot_api.services.redis_client import get_redis

_bot: Bot | None = None
_dispatcher: Dispatcher | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        settings = get_settings()
        _bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        storage = RedisStorage(redis=get_redis())
        _dispatcher = Dispatcher(storage=storage)
        _dispatcher.include_router(start.router)
        _dispatcher.include_router(sites.router)
        _dispatcher.include_router(manage.router)
        _dispatcher.include_router(onboarding.router)
        _dispatcher.include_router(edit.router)
        _dispatcher.errors.register(errors.on_error)
    return _dispatcher
