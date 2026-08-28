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
import re
import uuid

from aiogram import F, Router
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

from bot_api.bot.filters import has_text, is_declining
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
from bot_api.services.storage import (
    DOCUMENT_TYPES,
    IMAGE_TYPES,
    VIDEO_TYPES,
    UploadRejected,
    check_size,
    upload_media,
)
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

# What the owner is asked once a picture has landed. Deliberately not a menu: a list of
# five buttons is the bot telling the owner which five things it can imagine, and an owner
# who wanted the picture somewhere else -- beside a particular service, further down the
# page -- had no way to say so and picked the closest wrong one instead. An open question
# gets an answer in their words, and their words can say anything.
_PLACEMENT_BODY = (
    "Whereabouts on your site would you like it?\n\n"
    "Just tell me in your own words — for example <i>\"at the top\"</i>, "
    "<i>\"use it as my logo\"</i>, <i>\"behind the heading\"</i>, or "
    "<i>\"next to my About text\"</i>. If you want it somewhere else entirely, say that "
    "and I'll work it out."
)
PLACEMENT_QUESTION = "Got your picture! " + _PLACEMENT_BODY
PLACEMENT_QUESTION_KNOWN = (
    "I've already got this picture on your site. " + _PLACEMENT_BODY
    + "\n\nWherever you pick, I'll move it there rather than adding a second copy."
)

# Read before any model call. Almost every answer to the question above is one of a few
# phrasings, and paying to have "as my logo" interpreted would be paying for nothing. An
# answer this cannot read is not a failure: it goes to FREEFORM_PLACEMENT below, where the
# owner's own words become the instruction.
#
# Order matters: `background` is checked before `hero` because "behind the text at the top"
# contains "top", and the more specific reading is the right one.
_PLACEMENT_PATTERNS = (
    ("background", re.compile(
        r"\bbehind\b|\bbackground\b|\bback of\b|\bunder the (?:text|heading|title)\b",
        re.IGNORECASE)),
    ("logo", re.compile(r"\blogo\b|\bbrand(?:ing)? ?mark\b|\bicon\b", re.IGNORECASE)),
    ("gallery", re.compile(r"\bgaller(?:y|ies)\b|\bphotos? section\b|\bslideshow\b",
                           re.IGNORECASE)),
    ("about", re.compile(r"\babout\b", re.IGNORECASE)),
    ("hero", re.compile(
        r"\b(?:at|on|near|to) the top\b|\bhero\b|\bbanner\b|\bheader image\b|"
        r"\btop of (?:the|my) (?:page|site|website)\b|\bvery top\b|\bbig picture\b",
        re.IGNORECASE)),
)


# For a spot this module has no template of its own for. The owner's words are the
# instruction; everything else here exists to stop the model reaching for a URL it invented
# or leaving two pictures where the owner asked for one.
FREEFORM_PLACEMENT = (
    "The owner has just sent a photograph and said where they want it. Their exact words:\n"
    '"{words}"\n\n'
    "The photograph is already uploaded and lives at this address:\n{url}\n\n"
    "Put it where they asked, as <img src=\"{url}\" alt=\"{name}\"> with a class that suits "
    "where it lands. Rules:\n"
    "- Use that URL character for character. Never edit it and never invent another one; an "
    "<img> whose address does not load fails the build.\n"
    "- If they asked for a picture that is already there to be replaced or removed, remove "
    "that <img> rather than leaving both on the page.\n"
    "- If they counted sections (\"the 2nd section\"), count the visible <section> blocks "
    "down the page in order, not counting the header or the footer.\n"
    "- Change nothing else on the page."
)


def _page_named_in(words: str, pages: list[str]) -> list[str]:
    """The page the owner named, or the home page.

    A section is almost always on the home page, which is why that is the fallback -- but
    "put it on the contact page" says otherwise, and sending that to index.html would edit
    the wrong file and appear to do nothing.
    """
    for page in pages:
        stem = page.removesuffix(".html")
        if stem != "index" and re.search(rf"\b{stem}\b", words or "", re.IGNORECASE):
            return [page]
    return [pages[0]]


def placement_from_text(text: str) -> str | None:
    """Which of the known spots the owner named, or None if they meant something else.

    None is not a failure. It means the answer was more specific than the five slots this
    module knows how to build -- "beside the price list", "under the opening hours" -- and
    that belongs in the edit pipeline, which writes an instruction rather than picking from
    a list.
    """
    for placement, pattern in _PLACEMENT_PATTERNS:
        if pattern.search(text or ""):
            return placement
    return None


async def _ask_where(message: Message, business_id, url: str, *, already_known: bool = False):
    """Ask where the picture goes, and record that it arrived.

    The turn is written the moment the picture lands rather than when it is finally placed.
    An owner who sent a photo and then typed "put that one in the background" was asking
    about something the conversation had no record of: the upload happened here and left no
    trace anywhere the edit parser could see, so the bot asked "which photo?" about a
    picture it was holding at that moment.
    """
    text = PLACEMENT_QUESTION_KNOWN if already_known else PLACEMENT_QUESTION
    await push_edit_turn(
        get_redis(), business_id, "(sent a photo)",
        {"photo_url": url, "bot_asked": text},
    )
    await message.answer(text)


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
        await _ask_where(message, business.id, known.url, already_known=True)
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
    await _ask_where(message, business.id, uploaded["url"])


async def _place_photo(
    reply_to: Message, telegram_user_id: int, choice: str | None = None, words: str = "",
) -> None:
    """Put the pending picture where the owner said, and queue the change.

    `choice` is one of the five spots this module builds itself. When it is None the owner
    described somewhere else -- "in the 2nd section", "under the opening hours" -- and their
    own words become the instruction instead.

    That second case used to hand the message to the edit pipeline, which knew nothing about
    the picture being held here and asked whether they had one. A real owner answered "Put
    this image in 2 section and remove current one" and was asked to send a photo, seconds
    after sending it.
    """
    redis = get_redis()
    pending = await get_pending_photo(redis, telegram_user_id)
    if pending is None:
        await reply_to.answer("That picture has expired — please send it again.")
        return

    business_id = uuid.UUID(pending["business_id"])
    media_type = pending.get("media_type", "photo")

    if media_type == "video":
        # One templated spot, because "at the top" is the only place asked for often
        # enough to be worth fixing in advance. Everything else goes through the
        # free-form instruction, which can put it anywhere on the page.
        kind = "video"
        instruction_template = (
            VIDEO_HERO_INSTRUCTION if _VIDEO_TOP_RE.search(words or "")
            else VIDEO_FREEFORM_INSTRUCTION
        )
    elif media_type == "document":
        kind, instruction_template = "document", DOCUMENT_INSTRUCTION
    elif choice is not None:
        kind, instruction_template = PLACEMENTS[choice]
    else:
        kind, instruction_template = "photo", FREEFORM_PLACEMENT

    async with session_scope() as session:
        business = await get_business_by_id(session, business_id, telegram_user_id)
        if business is None:
            await clear_pending_photo(redis, telegram_user_id)
            await reply_to.answer("Couldn't find that site — use /mysites to pick one.")
            return

        reuse = bool(pending.get("reuse"))

        if kind == "logo":
            # Only one logo: replace any previous one rather than stacking them up.
            for existing in [m for m in business.media if m.kind == "logo"]:
                await session.delete(existing)
        elif kind in ("video", "document"):
            # No cap of their own yet, and no de-duplication: a second PDF is usually a
            # second document (a menu and a wine list), not the same one sent twice.
            pass
        elif not reuse and len([m for m in business.media if m.kind == "photo"]) >= MAX_GALLERY_PHOTOS:
            await clear_pending_photo(redis, telegram_user_id)
            await reply_to.answer(
                f"You've already got {MAX_GALLERY_PHOTOS} pictures on <b>{business.name}</b> — "
                "remove one before adding another."
            )
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

    await clear_pending_photo(redis, telegram_user_id)

    # The logo appears in the header of every page, so it has to be patched into all of
    # them; the others only touch the page they sit on. A free-form placement can name a
    # page of its own ("on the contact page"), so it gets to say.
    pages = list(page_files_for(layout))
    if choice == "logo":
        targets = pages
    elif media_type in ("video", "document"):
        targets = _page_named_in(words, pages)
    elif choice is not None:
        targets = [pages[0]]
    else:
        targets = _page_named_in(words, pages)

    await enqueue_generation(
        business_id, trigger="edit",
        patch={
            "instruction": instruction_template.format(
                url=pending["url"], name=name, words=words.strip(),
                filename=pending.get("filename", "")),
            "targets": targets,
            "user_message": words or f"(placed a photo: {choice})",
        },
    )
    # Put the picture into the same conversation memory the chat editor reads. Without
    # this, an owner who uploaded a photo and then said "put that picture in the
    # background" got asked "which photo?" -- the upload had happened in a completely
    # separate handler that left no trace of itself anywhere the parser could see.
    noun = {"video": "video", "document": "PDF"}.get(media_type, "picture")
    if media_type in ("video", "document") or choice is None:
        doing = f'putting it where you said: "{words.strip()}"'
    else:
        doing = CONFIRMATION[choice]
    await push_edit_turn(
        redis, business_id, f"(sent a {noun})",
        {"photo_url": pending["url"], "applied": media_type,
         "summary": f"{doing} — the {noun} is at {pending['url']}"},
    )
    await reply_to.answer(f"Great — {doing}. I'll message you when it's live!")


async def _has_pending_photo(message: Message) -> bool:
    """True while a picture is sitting here waiting to be told where it goes.

    A filter rather than a check inside the handler, because this router is registered
    ahead of the edit router: a handler that matched every message and then decided it had
    nothing to do would swallow the message rather than let the editor see it.
    """
    return (await get_pending_photo(get_redis(), message.from_user.id)) is not None


@router.message(default_state, has_text, _has_pending_photo)
async def on_placement_reply(message: Message) -> None:
    """The owner saying where the picture they just sent should go.

    Answers this handler cannot read are not errors and are not bounced back with the
    question repeated. They are handed to the edit pipeline with the picture's URL already
    in the conversation buffer, which can write an instruction for a spot this module has
    no button for -- "under the opening hours", "next to the second service".
    """
    redis = get_redis()

    if is_declining(message.text):
        await clear_pending_photo(redis, message.from_user.id)
        await message.answer("No problem — I've left your site as it is.")
        return

    # The five templated spots are all about photographs -- a logo, a gallery, behind the
    # heading. Matching a video or a PDF against them would put a menu in the photo
    # gallery, so those go straight to their own instruction with the owner's words.
    pending = await get_pending_photo(redis, message.from_user.id)
    if pending and pending.get("media_type") in ("video", "document"):
        await _place_photo(message, message.from_user.id, words=message.text)
        return

    choice = placement_from_text(message.text)
    if choice is not None:
        await _place_photo(message, message.from_user.id, choice)
        return

    # Somewhere this module has no template for -- "in the 2nd section", "under the opening
    # hours". Their words become the instruction and the picture goes in anyway.
    #
    # This used to hand the message on to the edit pipeline instead, which knew nothing
    # about the picture being held here: an owner who wrote "Put this image in 2 section and
    # remove current one" was asked whether they had a photograph to send, seconds after
    # sending one. Passing the message along was never the right move -- the answer to
    # "where would you like it?" belongs to whoever asked the question.
    logger.info(
        "photo.placement_freeform",
        extra={"event": "photo.placement_freeform", "text": message.text[:120]},
    )
    await _place_photo(message, message.from_user.id, words=message.text)


@router.callback_query(F.data.startswith("photo:"))
async def on_photo_placement(callback: CallbackQuery) -> None:
    """Kept for the buttons older messages are still showing.

    New uploads ask the question in words instead, so nothing produces these any more --
    but a keyboard already sitting in someone's chat history stays tappable, and a button
    that silently does nothing is worse than one that still works.
    """
    choice = callback.data.split(":", 1)[1]

    if choice == "cancel":
        await clear_pending_photo(get_redis(), callback.from_user.id)
        await callback.message.answer("No problem — I've left your site as it is.")
        await callback.answer()
        return

    if choice in PLACEMENTS:
        await _place_photo(callback.message, callback.from_user.id, choice)
    await callback.answer()


# --------------------------------------------------------------- video and documents

# A video and a PDF land in the same pending slot as a photograph and are answered with
# the same open question, because the owner's problem is identical -- "I have a file, put
# it somewhere on my site" -- and a second parallel flow would be a second place for the
# same bugs to live.

VIDEO_PLACEMENT_QUESTION = (
    "Got your video! Whereabouts on your site would you like it?\n\n"
    "Tell me in your own words — for example <i>\"at the top\"</i>, "
    "<i>\"in the about section\"</i>, or <i>\"near the bottom of the page\"</i>.\n\n"
    "<i>It won't play by itself — visitors tap to start it, which is what keeps your page "
    "quick to load on a phone.</i>"
)

VIDEO_HERO_INSTRUCTION = (
    "Show this video near the top of the page, just below the hero heading and its text. "
    'Use exactly:\n<div class="video-wrap"><video class="site-video" controls '
    'preload="metadata" playsinline src="{url}"></video></div>\n'
    "Rules: never add autoplay, never add loop, and never add muted-autoplay. The video "
    "must not start on its own -- a video that plays by itself on a phone costs the "
    "visitor their data and is the fastest way to make them leave. If a video is already "
    "on this page, REPLACE it with this one rather than adding a second. Change nothing "
    "else."
)

VIDEO_FREEFORM_INSTRUCTION = (
    "The owner has just sent a video and said where they want it. Their exact words:\n"
    '"{words}"\n\n'
    "The video is already uploaded and lives at this address:\n{url}\n\n"
    "Put it where they asked, using exactly:\n"
    '<div class="video-wrap"><video class="site-video" controls preload="metadata" '
    'playsinline src="{url}"></video></div>\n'
    "Rules:\n"
    "- Use that URL character for character. Never edit it and never invent another one.\n"
    "- Never add autoplay, loop or muted autoplay. The visitor presses play.\n"
    "- If a video is already on the page and they asked to change or move it, replace it "
    "rather than leaving two.\n"
    '- If they counted sections ("the 2nd section"), count the visible <section> blocks '
    "down the page in order, not counting the header or the footer.\n"
    "- Change nothing else on the page."
)

DOCUMENT_PLACEMENT_QUESTION = (
    "Got your PDF — <b>{filename}</b>.\n\n"
    "Whereabouts should the download link go, and what should it say?\n\n"
    "For example <i>\"put a Download our menu button in the middle of the page\"</i> or "
    "<i>\"add it to the contact section as our price list\"</i>. If you just say where, "
    "I'll label it sensibly."
)

DOCUMENT_INSTRUCTION = (
    "The owner has just uploaded a PDF and said where the download link should go. Their "
    'exact words:\n"{words}"\n\n'
    "The file is already uploaded and lives at this address:\n{url}\n\n"
    "Add a link to it where they asked, using exactly:\n"
    '<a class="doc-link" href="{url}" target="_blank" rel="noopener">LABEL</a>\n'
    "Rules:\n"
    "- Replace LABEL with what they called it. If they did not name it, write a plain "
    'label from the file name -- "{filename}" -- in ordinary words, e.g. "Download our '
    'menu". Never leave the word LABEL on the page.\n'
    "- Use that URL character for character. Never edit it and never invent another one.\n"
    "- target=\"_blank\" and rel=\"noopener\" are required so the visitor does not lose "
    "the page when they open it.\n"
    "- If a link to this same file is already on the page, move it rather than adding a "
    "second copy.\n"
    "- Change nothing else on the page."
)

# Only one spot is templated for a video -- the top of the page -- because that is the one
# place a video is asked for often enough to be worth a fixed template. Everything else
# goes through the free-form instruction, which can put it anywhere.
_VIDEO_TOP_RE = re.compile(
    r"\b(?:at|on|near|to) the top\b|\bhero\b|\bbanner\b|\bvery top\b|"
    r"\btop of (?:the|my) (?:page|site|website)\b",
    re.IGNORECASE,
)


def _clean_filename(name: str | None) -> str:
    """A file name safe to put in a storage path and to show to the owner."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "file").strip()).strip("-.")
    return (stem or "file")[:60]


async def _accept_upload(message: Message, kind: str, file_id: str, file_size: int | None,
                         filename: str, content_type: str, question: str) -> None:
    """Download, store and hold one file, then ask where it goes.

    Shared by video and PDF. Photographs keep their own handler because they carry the
    de-duplication path and five templated placements that neither of these needs.
    """
    business = await _resolve_business(message.from_user.id)
    if business is None:
        await message.answer(
            "Which site is that for? Use /mysites to pick one, or /newsite to build your "
            "first site."
        )
        return

    if is_business_busy(business):
        await message.answer(
            f"I'm still updating <b>{business.name}</b> — send that again in a minute."
        )
        return

    # Checked before the download, not after: Telegram reports the size on the message, so
    # an owner sending a 60MB video can be told immediately instead of waiting for a
    # transfer that was never going to be allowed to finish.
    try:
        check_size(kind, file_size)
    except UploadRejected as exc:
        await message.answer(str(exc))
        return

    await message.answer("Got it — one moment while I save that…")

    try:
        file = await message.bot.get_file(file_id)
        buffer = await message.bot.download_file(file.file_path)
        content = buffer.read()
    except Exception:
        logger.exception("%s download failed for business %s", kind, business.id)
        await message.answer(
            f"Sorry, I couldn't save that {kind} — please try sending it again."
        )
        return

    try:
        uploaded = await upload_media(business.id, kind, filename, content, content_type)
    except UploadRejected as exc:
        await message.answer(str(exc))
        return
    except Exception:
        logger.exception("%s upload failed for business %s", kind, business.id)
        await message.answer(
            f"Sorry, I couldn't save that {kind} — please try sending it again."
        )
        return

    await set_pending_photo(
        get_redis(), message.from_user.id,
        {"business_id": str(business.id), "url": uploaded["url"],
         "storage_path": uploaded["storage_path"], "media_type": kind,
         "filename": filename},
    )
    await push_edit_turn(
        get_redis(), business.id, f"(sent a {kind})",
        {"photo_url": uploaded["url"], "bot_asked": question},
    )
    await message.answer(question)


@router.message(default_state, F.video)
async def on_video(message: Message) -> None:
    video = message.video
    await _accept_upload(
        message, "video", video.file_id, video.file_size,
        _clean_filename(video.file_name or f"{video.file_unique_id}.mp4"),
        video.mime_type or "video/mp4",
        VIDEO_PLACEMENT_QUESTION,
    )


@router.message(default_state, F.document)
async def on_document(message: Message) -> None:
    """A file sent as a file rather than as a photo or a video.

    This is the handler that did not exist, and its absence was a silent dead end. Sending
    a picture "as a file" is something Telegram does by default from a computer, and an
    owner who did it got no reply of any kind -- no error, no explanation -- and no way to
    tell whether the bot was broken or ignoring them.
    """
    document = message.document
    mime = (document.mime_type or "").lower()
    filename = _clean_filename(document.file_name or document.file_unique_id)

    if mime in IMAGE_TYPES:
        # A photograph that happened to be sent uncompressed. It is a photograph.
        await _accept_upload(
            message, "photo", document.file_id, document.file_size, filename, mime,
            PLACEMENT_QUESTION,
        )
        return

    if mime in DOCUMENT_TYPES:
        await _accept_upload(
            message, "document", document.file_id, document.file_size, filename, mime,
            DOCUMENT_PLACEMENT_QUESTION.format(filename=filename),
        )
        return

    if mime in VIDEO_TYPES:
        await _accept_upload(
            message, "video", document.file_id, document.file_size, filename, mime,
            VIDEO_PLACEMENT_QUESTION,
        )
        return

    await message.answer(UNSUPPORTED_FILE_REPLY)


# Anything else a person can attach to a Telegram message: voice notes, music, stickers,
# contacts, locations. None of them can go on a website, but every one of them used to be
# met with total silence, which reads as a broken bot rather than an unsupported file.
UNSUPPORTED_FILE_REPLY = (
    "I can't put that sort of file on a website, I'm afraid.\n\n"
    "What I can use:\n"
    "• <b>Photos</b> — JPEG, PNG or WebP, up to 20MB\n"
    "• <b>Videos</b> — MP4, WebM or MOV, up to 20MB\n"
    "• <b>PDFs</b> — menus, price lists, brochures, up to 5MB\n\n"
    "Send me one of those and I'll ask where you'd like it. Or just tell me what you'd "
    "like changed on your site in your own words."
)


@router.message(default_state, F.audio | F.voice | F.sticker | F.animation
                | F.video_note | F.contact | F.location | F.poll)
async def on_unsupported_file(message: Message) -> None:
    await message.answer(UNSUPPORTED_FILE_REPLY)
