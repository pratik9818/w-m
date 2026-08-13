import re
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot_api.bot.filters import has_text
from bot_api.bot.keyboards import (
    add_another_service_keyboard,
    category_keyboard,
    confirm_keyboard,
    theme_keyboard,
)
from bot_api.bot.states.onboarding import MAX_PHOTOS, MAX_SERVICES, OnboardingStates
from bot_api.services.business_service import OnboardingSpec, create_business_from_spec
from bot_api.services.redis_client import get_redis
from bot_api.services.session import set_active_business
from bot_api.services.storage import UploadRejected, upload_media
from db.base import session_scope

router = Router(name="onboarding")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def begin_onboarding(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(business_id=str(uuid.uuid4()), services=[], photos=[])
    await state.set_state(OnboardingStates.waiting_name)
    await message.answer(
        "Let's build your website — I'll ask a few quick questions.\n\nWhat's your business name?"
    )


# ---- name ----


@router.message(OnboardingStates.waiting_name, has_text)
async def on_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if len(name) > 80:
        await message.answer("That's a bit long — please keep it under 80 characters.")
        return
    await state.update_data(name=name)
    await state.set_state(OnboardingStates.waiting_category)
    await message.answer("What category is your business?", reply_markup=category_keyboard())


# ---- category ----


@router.callback_query(OnboardingStates.waiting_category, F.data.startswith("category:"))
async def on_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":", 1)[1]
    if category == "Other":
        await state.set_state(OnboardingStates.waiting_category_other)
        await callback.message.answer("What's your business category?")
        await callback.answer()
        return

    await state.update_data(category=category)
    await state.set_state(OnboardingStates.waiting_tagline)
    await callback.message.answer(
        "Got it. Want a short one-line tagline? (or send /skip)"
    )
    await callback.answer()


@router.message(OnboardingStates.waiting_category_other, has_text)
async def on_category_other(message: Message, state: FSMContext) -> None:
    category = message.text.strip()[:40]
    await state.update_data(category=category)
    await state.set_state(OnboardingStates.waiting_tagline)
    await message.answer("Got it. Want a short one-line tagline? (or send /skip)")


# ---- tagline ----


@router.message(OnboardingStates.waiting_tagline, Command("skip"))
async def on_tagline_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(tagline=None)
    await state.set_state(OnboardingStates.waiting_about)
    await message.answer("Give me a short paragraph about your business (a couple sentences is fine).")


@router.message(OnboardingStates.waiting_tagline, has_text)
async def on_tagline(message: Message, state: FSMContext) -> None:
    tagline = message.text.strip()
    if len(tagline) > 120:
        await message.answer("Keep it under 120 characters, please.")
        return
    await state.update_data(tagline=tagline)
    await state.set_state(OnboardingStates.waiting_about)
    await message.answer("Give me a short paragraph about your business (a couple sentences is fine).")


# ---- about ----


@router.message(OnboardingStates.waiting_about, has_text)
async def on_about(message: Message, state: FSMContext) -> None:
    about = message.text.strip()
    if len(about) > 600:
        await message.answer("That's a bit long — please keep it under 600 characters.")
        return
    await state.update_data(about=about)
    await state.set_state(OnboardingStates.waiting_service_name)
    await message.answer(
        "Now let's list what you offer. Name your first service or product (or send /done if you'd rather skip this)."
    )


# ---- services loop ----


@router.message(OnboardingStates.waiting_service_name, Command("done"))
async def on_services_done_early(message: Message, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.waiting_phone)
    await message.answer("What's your phone number? (or /skip)")


@router.message(OnboardingStates.waiting_service_name, has_text)
async def on_service_name(message: Message, state: FSMContext) -> None:
    await state.update_data(pending_service_name=message.text.strip()[:120])
    await state.set_state(OnboardingStates.waiting_service_price)
    await message.answer("What's the price? (or /skip)")


@router.message(OnboardingStates.waiting_service_price, Command("skip"))
async def on_service_price_skip(message: Message, state: FSMContext) -> None:
    await _append_service(message, state, price_label=None)


@router.message(OnboardingStates.waiting_service_price, has_text)
async def on_service_price(message: Message, state: FSMContext) -> None:
    await _append_service(message, state, price_label=message.text.strip()[:60])


async def _append_service(message: Message, state: FSMContext, price_label: str | None) -> None:
    data = await state.get_data()
    services = data.get("services", [])
    services.append({"name": data["pending_service_name"], "price_label": price_label})
    await state.update_data(services=services, pending_service_name=None)

    if len(services) >= MAX_SERVICES:
        await state.set_state(OnboardingStates.waiting_phone)
        await message.answer(f"That's {MAX_SERVICES} services — let's move on. What's your phone number? (or /skip)")
        return

    await state.set_state(OnboardingStates.waiting_add_another_service)
    await message.answer("Added! Want to add another?", reply_markup=add_another_service_keyboard())


@router.callback_query(OnboardingStates.waiting_add_another_service, F.data == "service:add_another")
async def on_add_another_service(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.waiting_service_name)
    await callback.message.answer("Name your next service or product.")
    await callback.answer()


@router.callback_query(OnboardingStates.waiting_add_another_service, F.data == "service:done")
async def on_services_done(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.waiting_phone)
    await callback.message.answer("What's your phone number? (or /skip)")
    await callback.answer()


# ---- contact fields ----


@router.message(OnboardingStates.waiting_phone, Command("skip"))
async def on_phone_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=None)
    await state.set_state(OnboardingStates.waiting_email)
    await message.answer("What's your contact email? (or /skip)")


@router.message(OnboardingStates.waiting_phone, has_text)
async def on_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.text.strip()[:40])
    await state.set_state(OnboardingStates.waiting_email)
    await message.answer("What's your contact email? (or /skip)")


@router.message(OnboardingStates.waiting_email, Command("skip"))
async def on_email_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(email=None)
    await state.set_state(OnboardingStates.waiting_address)
    await message.answer("What's your business address? (or /skip)")


@router.message(OnboardingStates.waiting_email, has_text)
async def on_email(message: Message, state: FSMContext) -> None:
    email = message.text.strip()
    if not EMAIL_RE.match(email):
        await message.answer("That doesn't look like a valid email — try again, or send /skip.")
        return
    await state.update_data(email=email[:120])
    await state.set_state(OnboardingStates.waiting_address)
    await message.answer("What's your business address? (or /skip)")


@router.message(OnboardingStates.waiting_address, Command("skip"))
async def on_address_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(address=None)
    await state.set_state(OnboardingStates.waiting_hours)
    await message.answer(
        "What are your hours? e.g. 'Mon-Fri 9-6, Sat 10-2, closed Sun'"
    )


@router.message(OnboardingStates.waiting_address, has_text)
async def on_address(message: Message, state: FSMContext) -> None:
    await state.update_data(address=message.text.strip())
    await state.set_state(OnboardingStates.waiting_hours)
    await message.answer("What are your hours? e.g. 'Mon-Fri 9-6, Sat 10-2, closed Sun'")


# ---- hours ----


@router.message(OnboardingStates.waiting_hours, has_text)
async def on_hours(message: Message, state: FSMContext) -> None:
    await state.update_data(hours_display_text=message.text.strip()[:300])
    await state.set_state(OnboardingStates.waiting_logo)
    await message.answer("Send your logo as a photo (or /skip).")


# ---- logo ----


@router.message(OnboardingStates.waiting_logo, Command("skip"))
async def on_logo_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(logo=None)
    await state.set_state(OnboardingStates.waiting_photos)
    await message.answer(f"Send up to {MAX_PHOTOS} photos of your business, then send /done.")


@router.message(OnboardingStates.waiting_logo, F.photo)
async def on_logo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        uploaded = await _download_and_upload(message, uuid.UUID(data["business_id"]), "logo")
    except UploadRejected as exc:
        await message.answer(str(exc))
        return
    await state.update_data(logo=uploaded)
    await state.set_state(OnboardingStates.waiting_photos)
    await message.answer(f"Got it! Now send up to {MAX_PHOTOS} photos of your business, then send /done.")


@router.message(OnboardingStates.waiting_logo)
async def on_logo_invalid(message: Message) -> None:
    await message.answer("Please send a photo, or /skip.")


# ---- photos ----


@router.message(OnboardingStates.waiting_photos, Command("done"))
async def on_photos_done(message: Message, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.waiting_theme)
    await message.answer("Last step — pick a look for your site:", reply_markup=theme_keyboard())


@router.message(OnboardingStates.waiting_photos, F.photo)
async def on_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= MAX_PHOTOS:
        await message.answer(f"You've already sent {MAX_PHOTOS} photos — send /done to continue.")
        return

    try:
        uploaded = await _download_and_upload(message, uuid.UUID(data["business_id"]), "photo")
    except UploadRejected as exc:
        await message.answer(str(exc))
        return

    photos.append(uploaded)
    await state.update_data(photos=photos)

    if len(photos) >= MAX_PHOTOS:
        await state.set_state(OnboardingStates.waiting_theme)
        await message.answer(f"That's {MAX_PHOTOS} photos. Now pick a look for your site:", reply_markup=theme_keyboard())
        return

    await message.answer(f"Got it ({len(photos)}/{MAX_PHOTOS}). Send another, or /done.")


@router.message(OnboardingStates.waiting_photos)
async def on_photos_invalid(message: Message) -> None:
    await message.answer("Please send a photo, or /done to continue.")


async def _download_and_upload(message: Message, business_id: uuid.UUID, kind: str) -> dict:
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buffer = await message.bot.download_file(file.file_path)
    content = buffer.read()
    filename = file.file_path.rsplit("/", 1)[-1]
    content_type = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return await upload_media(business_id, kind, filename, content, content_type)


# ---- theme ----


@router.callback_query(OnboardingStates.waiting_theme, F.data.startswith("theme:"))
async def on_theme(callback: CallbackQuery, state: FSMContext) -> None:
    theme = callback.data.split(":", 1)[1]
    await state.update_data(theme=theme)
    await state.set_state(OnboardingStates.confirm)

    data = await state.get_data()
    summary = _build_summary(data)
    await callback.message.answer(summary, reply_markup=confirm_keyboard())
    await callback.answer()


def _build_summary(data: dict) -> str:
    services = data.get("services", [])
    services_text = ", ".join(s["name"] for s in services) or "(none listed)"
    return (
        "Here's what I've got:\n\n"
        f"<b>{data.get('name')}</b>\n"
        f"Category: {data.get('category')}\n"
        f"Tagline: {data.get('tagline') or '(none)'}\n"
        f"About: {data.get('about')}\n"
        f"Services: {services_text}\n"
        f"Phone: {data.get('phone') or '(none)'}\n"
        f"Email: {data.get('email') or '(none)'}\n"
        f"Address: {data.get('address') or '(none)'}\n"
        f"Hours: {data.get('hours_display_text') or '(none)'}\n"
        f"Theme: {data.get('theme')}\n"
        f"Photos: {len(data.get('photos', []))}\n\n"
        "Ready to create your site?"
    )


# ---- confirm ----


@router.callback_query(OnboardingStates.confirm, F.data == "onboarding:restart")
async def on_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await begin_onboarding(callback.message, state)


@router.callback_query(OnboardingStates.confirm, F.data == "onboarding:confirm")
async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    spec = OnboardingSpec()
    spec.business_id = uuid.UUID(data["business_id"])
    spec.name = data["name"]
    spec.category = data["category"]
    spec.tagline = data.get("tagline")
    spec.about = data.get("about")
    spec.services = data.get("services", [])
    spec.phone = data.get("phone")
    spec.email = data.get("email")
    spec.address = data.get("address")
    spec.hours_display_text = data.get("hours_display_text")
    spec.logo = data.get("logo")
    spec.photos = data.get("photos", [])
    spec.theme = data.get("theme", "classic")

    async with session_scope() as session:
        business = await create_business_from_spec(session, callback.from_user.id, spec)

    await set_active_business(get_redis(), callback.from_user.id, business.id)
    await state.clear()

    await callback.message.answer(
        f"🎉 <b>{business.name}</b> is queued for building! "
        "I'll message you here with the live link as soon as it's ready.\n\n"
        "(Generation isn't wired up yet in this build — that's next.)"
    )
    await callback.answer()
