import json
import uuid

from redis.asyncio import Redis

_ACTIVE_BUSINESS_KEY = "active_business:{telegram_user_id}"
_ACTIVE_BUSINESS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

_EDIT_CONTEXT_KEY = "nl_edit_ctx:{business_id}"
_EDIT_CONTEXT_TTL_SECONDS = 600  # 10 min sliding window
_EDIT_CONTEXT_MAX_TURNS = 3

_PENDING_EDIT_KEY = "pending_edit:{business_id}"
_PENDING_EDIT_TTL_SECONDS = 600


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


async def get_edit_context(redis: Redis, business_id: uuid.UUID) -> list[dict]:
    """Last few NL-edit turns for this business, oldest first -- used to resolve
    short/ambiguous follow-ups ("yes", "that one") against the bot's own last
    question. `not_an_edit` turns aren't stored here (see push_edit_turn)."""
    key = _EDIT_CONTEXT_KEY.format(business_id=business_id)
    value = await redis.get(key)
    if value is None:
        return []
    return json.loads(value)


async def push_edit_turn(redis: Redis, business_id: uuid.UUID, raw_message: str, outcome: dict) -> None:
    key = _EDIT_CONTEXT_KEY.format(business_id=business_id)
    turns = await get_edit_context(redis, business_id)
    turns.append({"raw_message": raw_message, "outcome": outcome})
    turns = turns[-_EDIT_CONTEXT_MAX_TURNS:]
    await redis.set(key, json.dumps(turns), ex=_EDIT_CONTEXT_TTL_SECONDS)


async def get_pending_edit(redis: Redis, business_id: uuid.UUID) -> dict | None:
    key = _PENDING_EDIT_KEY.format(business_id=business_id)
    value = await redis.get(key)
    return json.loads(value) if value is not None else None


async def set_pending_edit(redis: Redis, business_id: uuid.UUID, op: dict) -> None:
    key = _PENDING_EDIT_KEY.format(business_id=business_id)
    await redis.set(key, json.dumps(op), ex=_PENDING_EDIT_TTL_SECONDS)


async def clear_pending_edit(redis: Redis, business_id: uuid.UUID) -> None:
    key = _PENDING_EDIT_KEY.format(business_id=business_id)
    await redis.delete(key)
