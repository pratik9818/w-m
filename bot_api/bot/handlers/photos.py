"""Send a photo at any time; the bot asks where it should go, then puts it there.

Image upload used to be two questions inside onboarding. When that flow was replaced by a
single free-text brief, uploading became impossible -- the generator still supported
logo/photo URLs but nothing could populate them. This restores it in a form that fits the
new model: no question up front, just send the picture whenever you have one.

The owner is always asked where it goes rather than the bot guessing, because a logo and a
gallery photo are completely different things and getting it wrong is very visible.
"""
import hashlib
import logging
import uuid

from aiogram import F, Router
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

from bot_api.bot.keyboards import photo_placement_keyboard
from bot_api.services.business_service import get_business_by_id, list_businesses_for_owner
from bot_api.services.edit_ops import is_business_busy
from bot_api.services.queue import enqueue_generation
from bot_api.services.redis_client import get_redis
from bot_api.services.session import (
    clear_pending_photo,
    get_active_business_id,
    get_pending_photo,
    push_edit_turn,
    set_pending_photo,
)
from bot_api.services.storage import UploadRejected, upload_media
from db.base import session_scope
from db.models import Media
from worker.codegen.builder import page_files_for

logger = logging.getLogger(__name__)
router = Router(name="photos")

MAX_GALLERY_PHOTOS = 8

# Where the picture goes -> how to describe that to the model, and which page it belongs on.
# "hero"/"about" land on the home page; a gallery photo goes wherever photos are shown.
PLACEMENTS = {
    "logo": (
        "logo",
        "Put this image in the site header as the business logo, next to the business name: "
        '<img src="{url}" alt="{name} logo" class="logo-mark">. Keep it small and do not '
        "change anything else.",
    ),
    "hero": (
        "photo",
        "Show this image as the large banner picture at the very top of the page, inside "
        'the hero section: <img src="{url}" alt="{name}" class="hero-image">. There must '
        "end up with exactly ONE picture at the top: if the hero section already has one, "
        "REPLACE it with this one instead of adding a second, and if the hero has a "
        "background image remove that too. Do not change anything else.",
    ),
    # Distinct from "hero" above: that puts the picture above the text, this puts it behind
    # it. An owner asked for exactly this in chat twice and got nothing both times, because
    # there was no way to say it -- offering it as a choice here is the direct answer.
    "background": (
        "photo",
        "Put this image BEHIND the text at the top of the page as a background, not above "
        'it. Add the class `hero-bg` alongside `hero` on the hero section and set '
        "style=\"background-image: url('{url}')\" on that same element. Remove EVERY "
        '<img class="hero-image"> from the hero section, whatever picture it shows, so the '
        "photo does not appear twice. If the hero already has a background image, replace "
        "it with this one. The overlay that keeps the text readable is already in the "
        "stylesheet — do not add one. Do not change anything else.",
    ),
    "gallery": (
        "photo",
        "Show this image in the photo gallery: <img src=\"{url}\" alt=\"{name}\" "
        'class="gallery-image">. If there is no gallery yet, add a new section with the '
        'heading "Gallery" containing a `card-grid` with this one image in it. If this '
        "exact image already appears anywhere else on the page, move it into the gallery "
        "rather than leaving a second copy behind. Do not change anything else.",
    ),
    "about": (
        "photo",
        "Show this image beside the About text: <img src=\"{url}\" alt=\"{name}\" "
        'class="about-image">. If a picture is already there, REPLACE it with this one '
        "rather than adding a second. Do not change anything else.",
    ),
}

CONFIRMATION = {
    "logo": "using it as your logo",
    "hero": "putting it at the top of your page",
    "background": "putting it behind the text at the top of your page",
    "gallery": "adding it to your photo gallery",
    "about": "putting it beside your About text",
}


async def _resolve_business(telegram_user_id: int):
    active_id = await get_active_business_id(get_redis(), telegram_user_id)
    async with session_scope() as session:
        if active_id is not None:
            business = await get_business_by_id(session, active_id, telegram_user_id)
            if business is not None:
                return business
        owned = await list_businesses_for_owner(session, telegram_user_id)
        return owned[0] if len(owned) == 1 else None


@router.message(default_state, F.photo)
async def on_photo(message: Message) -> None:
    business = await _resolve_business(message.from_user.id)
    if business is None:
        await message.answer(
            "Nice picture! Which site is it for? Use /mysites to pick one, or /newsite to "
            "build your first site."
        )
        return

    if is_business_busy(business):
        await message.answer(
            f"I'm still updating <b>{business.name}</b> — send the picture again in a minute."
        )
        return

    # Telegram sends several sizes; the last is the largest.
    photo = message.photo[-1]
    try:
        file = await message.bot.get_file(photo.file_id)
        buffer = await message.bot.download_file(file.file_path)
        content = buffer.read()
    except Exception:
        logger.exception("photo download failed for business %s", business.id)
        await message.answer("Sorry, I couldn't save that picture — please try sending it again.")
        return

    # Telegram issues a fresh file id every time a picture is uploaded again, so the id
    # cannot tell "the same photo" from "a new one" -- three real uploads of one image
    # produced three different ids. The bytes can.
    digest = hashlib.sha256(content).hexdigest()
    known = next((m for m in business.media if m.content_hash == digest), None)

    if known is not None:
        # Already on this site: reuse the stored file rather than uploading a second copy
        # of identical bytes and offering it as if it were new.
        await set_pending_photo(
            get_redis(), message.from_user.id,
            {"business_id": str(business.id), "url": known.url,
             "storage_path": known.storage_path, "hash": digest, "reuse": True},
        )
        await message.answer(
            "I've already got this picture on your site. Where would you like it? "
            "Picking a spot moves it there rather than adding a second copy.",
            reply_markup=photo_placement_keyboard(),
        )
        return

    try:
        uploaded = await upload_media(
            business.id, "photo", f"{photo.file_unique_id}.jpg", content, "image/jpeg"
        )
    except UploadRejected as exc:
        await message.answer(str(exc))
        return
    except Exception:
        logger.exception("photo upload failed for business %s", business.id)
        await message.answer("Sorry, I couldn't save that picture — please try sending it again.")
        return

    await set_pending_photo(
        get_redis(), message.from_user.id,
        {"business_id": str(business.id), "url": uploaded["url"],
         "storage_path": uploaded["storage_path"], "hash": digest},
    )
    await message.answer(
        "Got your picture! Where would you like it on your site?",
        reply_markup=photo_placement_keyboard(),
    )


@router.callback_query(F.data.startswith("photo:"))
async def on_photo_placement(callback: CallbackQuery) -> None:
    choice = callback.data.split(":", 1)[1]
    redis = get_redis()
    pending = await get_pending_photo(redis, callback.from_user.id)

    if pending is None:
        await callback.answer("That picture has expired — please send it again.", show_alert=True)
        return

    if choice == "cancel":
        await clear_pending_photo(redis, callback.from_user.id)
        await callback.message.answer("No problem — I've left your site as it is.")
        await callback.answer()
        return

    if choice not in PLACEMENTS:
        await callback.answer()
        return

    kind, instruction_template = PLACEMENTS[choice]
    business_id = uuid.UUID(pending["business_id"])

    async with session_scope() as session:
        business = await get_business_by_id(session, business_id, callback.from_user.id)
        if business is None:
            await clear_pending_photo(redis, callback.from_user.id)
            await callback.answer("Couldn't find that site.", show_alert=True)
            return

        reuse = bool(pending.get("reuse"))

        if kind == "logo":
            # Only one logo: replace any previous one rather than stacking them up.
            for existing in [m for m in business.media if m.kind == "logo"]:
                await session.delete(existing)
        elif not reuse and len([m for m in business.media if m.kind == "photo"]) >= MAX_GALLERY_PHOTOS:
            await clear_pending_photo(redis, callback.from_user.id)
            await callback.message.answer(
                f"You've already got {MAX_GALLERY_PHOTOS} pictures on <b>{business.name}</b> — "
                "remove one before adding another."
            )
            await callback.answer()
            return

        # A picture already on this site is being moved, not added: recording it a second
        # time is what put two copies of the same photo on a real owner's home page.
        if not reuse:
            session.add(Media(
                business_id=business.id, kind=kind,
                storage_path=pending["storage_path"], url=pending["url"],
                content_hash=pending.get("hash"),
            ))
        business.generation_status = "queued"
        await session.commit()
        name, layout = business.name, business.layout

    await clear_pending_photo(redis, callback.from_user.id)

    # The logo appears in the header of every page, so it has to be patched into all of
    # them; the others only touch the page they sit on.
    pages = list(page_files_for(layout))
    targets = pages if choice == "logo" else [pages[0]]

    await enqueue_generation(
        business_id, trigger="edit",
        patch={
            "instruction": instruction_template.format(url=pending["url"], name=name),
            "targets": targets,
        },
    )
    # Put the picture into the same conversation memory the chat editor reads. Without
    # this, an owner who uploaded a photo and then said "put that picture in the
    # background" got asked "which photo?" -- the upload had happened in a completely
    # separate handler that left no trace of itself anywhere the parser could see.
    await push_edit_turn(
        redis, business_id, "(sent a photo)",
        {"applied": "photo", "summary": f"{CONFIRMATION[choice]} — the picture is at {pending['url']}"},
    )
    await callback.message.answer(
        f"Great — {CONFIRMATION[choice]}. I'll message you when it's live!"
    )
    await callback.answer()
