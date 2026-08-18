import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot_api.bot.handlers.onboarding import begin_onboarding
from bot_api.bot.keyboards import sites_list_keyboard
from bot_api.services.business_service import get_business_by_id, list_businesses_for_owner
from bot_api.services.redis_client import get_redis
from bot_api.services.session import set_active_business
from db.base import session_scope

router = Router(name="sites")


@router.message(Command("newsite"))
async def cmd_newsite(message: Message, state: FSMContext) -> None:
    await begin_onboarding(message, state)


@router.callback_query(F.data == "menu:newsite")
async def cb_newsite(callback: CallbackQuery, state: FSMContext) -> None:
    await begin_onboarding(callback.message, state)
    await callback.answer()


async def _show_sites(message: Message, owner_telegram_id: int) -> None:
    async with session_scope() as session:
        businesses = await list_businesses_for_owner(session, owner_telegram_id)

    if not businesses:
        await message.answer("You don't have any sites yet. Use /newsite to create one.")
        return

    lines = []
    for business in businesses:
        link = business.deployment_url or "(still building)"
        lines.append(f"• <b>{business.name}</b> — {link}")

    await message.answer(
        "Your sites:\n" + "\n".join(lines) + "\n\nTap one below to make it active for editing:",
        reply_markup=sites_list_keyboard(businesses),
    )


@router.message(Command("mysites"))
async def cmd_mysites(message: Message) -> None:
    await _show_sites(message, message.from_user.id)


@router.callback_query(F.data == "menu:mysites")
async def cb_mysites(callback: CallbackQuery) -> None:
    await _show_sites(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("site:select:"))
async def cb_select_site(callback: CallbackQuery) -> None:
    business_id = uuid.UUID(callback.data.split(":")[-1])
    async with session_scope() as session:
        business = await get_business_by_id(session, business_id, callback.from_user.id)

    if business is None:
        await callback.answer("Couldn't find that site.", show_alert=True)
        return

    await set_active_business(get_redis(), callback.from_user.id, business_id)
    await callback.message.answer(
        f"You're now editing <b>{business.name}</b>. Send me any change you'd like to make."
    )
    await callback.answer()
