from aiogram import Router
from aiogram.fsm.state import default_state
from aiogram.types import Message

from bot_api.services.business_service import get_business_by_id, list_businesses_for_owner
from bot_api.services.edit_ops import ValidationError, apply_edit_operation, is_business_busy
from bot_api.services.nl_edit import EditParseFailed, parse_edit_message
from bot_api.services.queue import enqueue_generation
from bot_api.services.redis_client import get_redis
from bot_api.services.session import get_active_business_id
from db.base import session_scope
from db.models import EditLog

router = Router(name="edit")


def _is_not_a_command(message: Message) -> bool:
    return bool(message.text) and not message.text.startswith("/")


def _log(business_id, telegram_user_id: int, raw_message: str, *, op: dict | None = None,
         applied: bool = False, error: str | None = None) -> EditLog:
    return EditLog(
        business_id=business_id,
        telegram_user_id=telegram_user_id,
        raw_message=raw_message,
        parsed_operation=op,
        applied=applied,
        error=error,
    )


@router.message(default_state, _is_not_a_command)
async def catch_all_edit(message: Message) -> None:
    """Lowest-priority handler: any free text from a known owner outside an active
    FSM flow is treated as a natural-language edit request for their active site."""
    redis = get_redis()
    active_id = await get_active_business_id(redis, message.from_user.id)
    raw_message = message.text
    telegram_user_id = message.from_user.id

    async with session_scope() as session:
        if active_id is not None:
            business = await get_business_by_id(session, active_id, telegram_user_id)
        else:
            business = None

        if business is None:
            businesses = await list_businesses_for_owner(session, telegram_user_id)
            if len(businesses) == 1:
                business = businesses[0]

        if business is None:
            await message.answer(
                "Not sure what you'd like to do! Use /mysites to pick a site, or /newsite to build one."
            )
            return

        if is_business_busy(business):
            session.add(_log(
                business.id, telegram_user_id, raw_message,
                error=f"rejected: business busy (status={business.generation_status})",
            ))
            await session.commit()
            await message.answer(
                f"Hang tight — <b>{business.name}</b> is already being updated "
                f"(status: <b>{business.generation_status}</b>). Try again in a minute or two!"
            )
            return

        try:
            op = await parse_edit_message(raw_message, business)
        except EditParseFailed:
            session.add(_log(business.id, telegram_user_id, raw_message, error="edit parsing failed"))
            await session.commit()
            await message.answer("Sorry, I couldn't process that just now — please try again in a moment.")
            return

        if op["operation"] == "not_an_edit":
            session.add(_log(business.id, telegram_user_id, raw_message, op=op))
            await session.commit()
            await message.answer(
                "Not sure that's something I can help edit! If you want to change your site, just tell me "
                'what to update — e.g. "change my hours to 9-6". Use /mysites to switch sites or /newsite '
                "to build another."
            )
            return

        if op["operation"] == "clarify":
            session.add(_log(business.id, telegram_user_id, raw_message, op=op))
            await session.commit()
            await message.answer(op["question"])
            return

        try:
            summary = await apply_edit_operation(session, business, op)
        except ValidationError as exc:
            session.add(_log(business.id, telegram_user_id, raw_message, op=op, error=str(exc)))
            await session.commit()
            await message.answer(str(exc))
            return

        business.generation_status = "queued"
        session.add(_log(business.id, telegram_user_id, raw_message, op=op, applied=True))
        await session.commit()
        business_id, business_name = business.id, business.name

    await enqueue_generation(business_id, trigger="edit")
    await message.answer(
        f"Updating <b>{business_name}</b> — {summary}. I'll message you here once the new version is live!"
    )
