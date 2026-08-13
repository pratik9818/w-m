from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import Message

from bot_api.services.business_service import get_business_by_id, list_businesses_for_owner
from bot_api.services.redis_client import get_redis
from bot_api.services.session import get_active_business_id
from db.base import session_scope

router = Router(name="edit")


def _is_not_a_command(message: Message) -> bool:
    return bool(message.text) and not message.text.startswith("/")


@router.message(default_state, _is_not_a_command)
async def catch_all_edit(message: Message, state: FSMContext) -> None:
    """Lowest-priority handler: anything from a known owner that isn't a command or
    part of an active FSM flow. Natural-language editing is wired up in a later phase
    (Part 4) — for now this just orients the user."""
    redis = get_redis()
    active_id = await get_active_business_id(redis, message.from_user.id)

    async with session_scope() as session:
        if active_id is not None:
            business = await get_business_by_id(session, active_id, message.from_user.id)
        else:
            business = None

        if business is None:
            businesses = await list_businesses_for_owner(session, message.from_user.id)
            if len(businesses) == 1:
                business = businesses[0]

    if business is None:
        await message.answer(
            "Not sure what you'd like to do! Use /mysites to pick a site, or /newsite to build one."
        )
        return

    await message.answer(
        f"Got it — editing <b>{business.name}</b> by chatting is coming soon. "
        f"Right now its status is: <b>{business.generation_status}</b>."
    )
