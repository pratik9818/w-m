import logging
import re
import uuid

from aiogram import Router
from aiogram.fsm.state import default_state
from aiogram.types import Message

from bot_api.services.business_service import get_business_by_id, list_businesses_for_owner
from bot_api.services.edit_ops import (
    ValidationError,
    apply_edit_operation,
    is_business_busy,
    is_structural_request,
    normalize_patch_targets,
    patch_for_extra_instructions,
    patch_for_field_edit,
    patch_for_service_edit,
)
from bot_api.services.nl_edit import EditParseFailed, parse_edit_message
from bot_api.services.queue import enqueue_generation, enqueue_rollback
from worker.codegen.builder import page_files_for
from worker.codegen.quota import record_usage
from worker.tasks.deploy import delete_pages_project
from bot_api.services.redis_client import get_redis
from bot_api.services.session import (
    clear_pending_edit,
    get_active_business_id,
    get_edit_context,
    get_pending_edit,
    push_edit_turn,
    set_pending_edit,
)
from db.base import session_scope
from db.models import EditLog

logger = logging.getLogger(__name__)
router = Router(name="edit")

_AFFIRMATIONS = {"yes", "yep", "yeah", "sure", "go ahead", "looks good", "perfect", "publish it", "do it", "confirm", "ok", "okay"}
_PUNCT_RE = re.compile(r"[.!?]+$")


def _is_not_a_command(message: Message) -> bool:
    return bool(message.text) and not message.text.startswith("/")


def _is_affirmation(text: str) -> bool:
    normalized = _PUNCT_RE.sub("", text.strip().lower())
    return normalized in _AFFIRMATIONS


def _blast_radius(targets: list[str]) -> str:
    """Tell the owner what is and isn't being touched -- the whole point of patching."""
    if targets == ["style.css"]:
        return "your page content stays exactly as it is"
    pages = [t for t in targets if t.endswith(".html")]
    if len(pages) == 1:
        return f"your other pages and your design stay exactly as they are"
    return "your design stays exactly as it is"


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

        # Which files this particular site actually has. A landing site has only
        # index.html and style.css, so any patch aimed at about/services/contact.html
        # would otherwise reach the worker and fail the build outright.
        site_files = [*page_files_for(business.layout), "style.css"]

        # A previously-drafted tagline/about is waiting on a yes/no before publishing.
        pending = await get_pending_edit(redis, business.id)
        if pending is not None:
            if _is_affirmation(raw_message):
                await clear_pending_edit(redis, business.id)

                # Restores already-published bytes -- no model call, so nothing new can
                # creep in and it costs no quota.
                if pending["operation"] == "undo":
                    business.generation_status = "queued"
                    session.add(_log(business.id, telegram_user_id, raw_message, op=pending, applied=True))
                    await session.commit()
                    business_id, business_name = business.id, business.name
                    await enqueue_rollback(business_id, uuid.UUID(pending["version_id"]))
                    await message.answer(f"Rolling <b>{business_name}</b> back now — one moment.")
                    return

                if pending["operation"] == "delete_site":
                    project_name, business_name = business.cf_pages_project_name, business.name
                    session.add(_log(business.id, telegram_user_id, raw_message, op=pending, applied=True))
                    await session.delete(business)
                    await session.commit()
                    if project_name:
                        try:
                            await delete_pages_project(project_name)
                        except Exception:
                            # Never block the delete the owner asked for on Cloudflare
                            # tidy-up; an orphaned project is a housekeeping matter.
                            logger.exception("failed to delete Pages project %s", project_name)
                    await message.answer(
                        f"<b>{business_name}</b> has been deleted and its site taken offline."
                    )
                    return

                # A confirmed rebuild throws the current design away deliberately, so it
                # skips the spec-mutation path entirely and rebuilds from scratch.
                if pending["operation"] == "rebuild_site":
                    business.generation_status = "queued"
                    session.add(_log(business.id, telegram_user_id, raw_message, op=pending, applied=True))
                    await session.commit()
                    await push_edit_turn(redis, business.id, raw_message, {"applied": "rebuild_site"})
                    business_id, business_name = business.id, business.name
                    await enqueue_generation(business_id, trigger="rebuild")
                    await message.answer(
                        f"Starting a fresh build of <b>{business_name}</b> from scratch. "
                        "I'll message you when the new version is live!"
                    )
                    return
                try:
                    summary = await apply_edit_operation(session, business, pending)
                except ValidationError as exc:
                    session.add(_log(business.id, telegram_user_id, raw_message, op=pending, error=str(exc)))
                    await session.commit()
                    await push_edit_turn(redis, business.id, raw_message, {"rejected": str(exc)})
                    await message.answer(str(exc))
                    return

                business.generation_status = "queued"
                session.add(_log(business.id, telegram_user_id, raw_message, op=pending, applied=True))
                await session.commit()
                await push_edit_turn(
                    redis, business.id, raw_message, {"applied": pending["operation"], "summary": summary}
                )
                business_id, business_name = business.id, business.name
                await enqueue_generation(
                    business_id, trigger="edit",
                    patch=patch_for_field_edit(pending, available=site_files)
                )
                await message.answer(
                    f"Updating <b>{business_name}</b> — {summary}. I'll message you here once the new version is live!"
                )
                return
            # Not an affirmation -- abandon the draft (nothing was ever written to the DB) and
            # fall through to a normal parse; the draft stays visible in the context buffer below.
            await clear_pending_edit(redis, business.id)

        context = await get_edit_context(redis, business.id)
        await message.answer("🧠 Got it — thinking about that...")
        try:
            op, parse_usage = await parse_edit_message(raw_message, business, context)
        except EditParseFailed:
            session.add(_log(business.id, telegram_user_id, raw_message, error="edit parsing failed"))
            await session.commit()
            await message.answer("Sorry, I couldn't process that just now — please try again in a moment.")
            return

        # Billed regardless of what the message turns out to be: reading it cost tokens
        # even if it was chit-chat or an ambiguous request we bounce back.
        await record_usage(
            session, telegram_user_id, business.id, parse_usage["model"],
            parse_usage["input_tokens"], parse_usage["output_tokens"], kind="parse",
        )

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
            await push_edit_turn(redis, business.id, raw_message, {"bot_asked": op["question"]})
            await message.answer(op["question"])
            return

        # A surgical change to something already on the site. Nothing in the spec changes --
        # the instruction is applied straight to the stored files, so every page and every
        # style rule the owner didn't mention comes through byte-identical.
        # Changing how many pages exist is a rebuild, not a patch -- a patch is handed one
        # file and asked to return it, so it can never delete one.
        if op["operation"] == "change_layout":
            wanted = "landing" if str(op.get("layout", "")).lower().startswith("land") else "multipage"
            if business.layout == wanted:
                await message.answer(
                    f"<b>{business.name}</b> is already "
                    + ("a one-page landing site." if wanted == "landing" else "a four-page site.")
                )
                return
            business.layout = wanted
            business.generation_status = "queued"
            session.add(_log(business.id, telegram_user_id, raw_message, op=op, applied=True))
            await session.commit()
            business_id, business_name = business.id, business.name
            await enqueue_generation(business_id, trigger="rebuild")
            await message.answer(
                f"Rebuilding <b>{business_name}</b> as "
                + ("a single landing page — everything on one page, with the menu scrolling "
                   "to each section." if wanted == "landing"
                   else "a four-page site with separate About, Services and Contact pages.")
                + "\n\nThis writes the site fresh, so the wording and design will change. "
                "I'll message you when it's live."
            )
            return

        if op["operation"] == "patch_site":
            instruction = (op.get("instruction") or "").strip()
            targets = normalize_patch_targets(
                op.get("targets"), available=list(page_files_for(business.layout)) + ["style.css"]
            )

            # Refuse before spending anything. One such request cost 21,867 tokens across
            # three calls and changed nothing, because patching cannot delete a page.
            if is_structural_request(instruction) or is_structural_request(raw_message):
                session.add(_log(business.id, telegram_user_id, raw_message, op=op,
                                 error="rejected: structural request sent to patch_site"))
                await session.commit()
                await message.answer(
                    "To change how many pages your site has, tell me which you want:\n\n"
                    "• <b>a single landing page</b> — everything on one page, menu scrolls to sections\n"
                    "• <b>a four-page site</b> — separate About, Services and Contact pages\n\n"
                    "Just say which one and I'll rebuild it that way."
                )
                return
            if not instruction or not targets:
                session.add(_log(business.id, telegram_user_id, raw_message, op=op,
                                 error="patch_site missing instruction or valid targets"))
                await session.commit()
                await message.answer(
                    "I'm not sure which part of your site you mean — could you say which page "
                    "or section it's on?"
                )
                return

            business.generation_status = "queued"
            session.add(_log(business.id, telegram_user_id, raw_message, op=op, applied=True))
            await session.commit()
            await push_edit_turn(redis, business.id, raw_message,
                                 {"applied": "patch_site", "summary": instruction})
            business_id, business_name = business.id, business.name
            await enqueue_generation(
                business_id, trigger="edit",
                patch={"instruction": instruction, "targets": targets},
            )
            await message.answer(
                f"On it — {instruction[0].lower() + instruction[1:] if instruction else instruction}\n\n"
                f"Only that changes; {_blast_radius(targets)}. I'll message you when it's live!"
            )
            return

        # A full rebuild discards the current design, so it always asks first -- reusing the
        # same pending-confirmation mechanism as drafted copy below.
        if op["operation"] == "rebuild_site":
            await set_pending_edit(redis, business.id, op)
            session.add(_log(business.id, telegram_user_id, raw_message, op=op, applied=False))
            await session.commit()
            await push_edit_turn(redis, business.id, raw_message, {"bot_asked": "confirm rebuild"})
            await message.answer(
                f"Just to check — that will rebuild <b>{business.name}</b> from scratch with a "
                "brand-new design, replacing all four pages and the current look. Your saved "
                'details stay.\n\nReply "yes" to go ahead, or tell me what to change instead.'
            )
            return

        # Model-composed tagline/about text is gated behind a lightweight confirmation before
        # it ever touches the DB -- everything else (the owner's own literal words, and
        # update_extra_instructions) applies immediately as before.
        drafted_field = None
        if op["operation"] == "update_business_info" and op.get("drafted"):
            if "about" in op:
                drafted_field = "about"
            elif "tagline" in op:
                drafted_field = "tagline"
        if drafted_field is not None:
            drafted_text = op[drafted_field]
            await set_pending_edit(redis, business.id, op)
            session.add(_log(business.id, telegram_user_id, raw_message, op=op, applied=False))
            await session.commit()
            await push_edit_turn(
                redis, business.id, raw_message,
                {"drafted_but_unpublished": True, "field": drafted_field, "text": drafted_text},
            )
            await message.answer(
                f"Here's what I drafted for your {drafted_field}:\n\n{drafted_text}\n\n"
                'Reply "yes" to publish it, or tell me what to change.'
            )
            return

        try:
            summary = await apply_edit_operation(session, business, op)
        except ValidationError as exc:
            session.add(_log(business.id, telegram_user_id, raw_message, op=op, error=str(exc)))
            await session.commit()
            await push_edit_turn(redis, business.id, raw_message, {"rejected": str(exc)})
            await message.answer(str(exc))
            return

        business.generation_status = "queued"
        session.add(_log(business.id, telegram_user_id, raw_message, op=op, applied=True))
        await session.commit()
        await push_edit_turn(redis, business.id, raw_message, {"applied": op["operation"], "summary": summary})
        business_id, business_name = business.id, business.name
        operation = op["operation"]

    # A spec edit is also applied surgically to the live files. `patch` staying None (an
    # extra_instructions preference, which only takes effect on a rebuild) falls back to a
    # full build, which is correct -- there is nothing on the current pages to patch.
    if operation == "update_business_info":
        patch = patch_for_field_edit(op, available=site_files)
    elif operation in ("add_service", "update_service", "remove_service"):
        patch = patch_for_service_edit(summary, available=site_files)
    elif operation == "update_extra_instructions" and op.get("instructions"):
        patch = patch_for_extra_instructions(op["instructions"])
    else:
        patch = None

    await enqueue_generation(business_id, trigger="edit", patch=patch)
    scope = f" Only that changes; {_blast_radius(patch['targets'])}." if patch else ""
    await message.answer(
        f"Updating <b>{business_name}</b> — {summary}.{scope} "
        "I'll message you here once the new version is live!"
    )
