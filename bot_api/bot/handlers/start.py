from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot_api.bot.handlers.onboarding import begin_onboarding
from bot_api.bot.keyboards import main_menu_keyboard
from bot_api.services.business_service import list_businesses_for_owner
from db.base import session_scope

router = Router(name="start")

HELP_TEXT = (
    "Here's what I can do:\n\n"
    "/newsite — build a new website: just describe it in one message\n"
    "/mysites — see and switch between your sites\n"
    "/status — check whether your site is live, building, or had a problem\n"
    "/traffic — see how many people visited and where they found you\n"
    "/data — see the enquiries customers sent through your site\n"
    "/plan — see your plan, and how many changes you have left this month\n"
    "/upgrade — get more websites and more changes\n"
    "/undo — put your site back to how it was before the last change\n"
    "/delete — permanently delete a site and take it offline\n"
    "/cancel — cancel whatever you're in the middle of\n\n"
    "Once a site is live, just send me a message like \"change my hours to 9-6\" "
    "to update it — no need for a command. I only change the part you mention; the rest "
    "of your site stays exactly as it is.\n\n"
    "You can ask me about your visitors the same way — \"how many visits today?\", "
    "\"did anyone look at it yesterday?\", \"where are these people coming from?\"\n\n"
    "✉️ Say \"add a contact form\" and people can message you straight from your site. "
    "Every message lands here the moment it's sent, and \"give me my site data\" shows "
    "you all of them whenever you want.\n\n"
    "📷 You can also send me a photo any time and I'll ask where you'd like it on your site."
)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        businesses = await list_businesses_for_owner(session, message.from_user.id)

    if not businesses:
        await begin_onboarding(message, state)
        return

    await message.answer(
        f"Welcome back! You have {len(businesses)} site(s).",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:help")
@router.message(Command("help"))
async def cmd_help(event: Message | CallbackQuery) -> None:
    target = event.message if isinstance(event, CallbackQuery) else event
    await target.answer(HELP_TEXT)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Nothing to cancel.")
        return
    await state.clear()
    await message.answer("Cancelled. Send /start to see your options.")
