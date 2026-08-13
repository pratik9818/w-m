import uuid

from redis.asyncio import Redis

_ACTIVE_BUSINESS_KEY = "active_business:{telegram_user_id}"
_ACTIVE_BUSINESS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


async def set_active_business(redis: Redis, telegram_user_id: int, business_id: uuid.UUID) -> None:
    key = _ACTIVE_BUSINESS_KEY.format(telegram_user_id=telegram_user_id)
    await redis.set(key, str(business_id), ex=_ACTIVE_BUSINESS_TTL_SECONDS)


async def get_active_business_id(redis: Redis, telegram_user_id: int) -> uuid.UUID | None:
    key = _ACTIVE_BUSINESS_KEY.format(telegram_user_id=telegram_user_id)
    value = await redis.get(key)
    if value is None:
        return None
    return uuid.UUID(value if isinstance(value, str) else value.decode())


async def clear_active_business(redis: Redis, telegram_user_id: int) -> None:
    key = _ACTIVE_BUSINESS_KEY.format(telegram_user_id=telegram_user_id)
    await redis.delete(key)
