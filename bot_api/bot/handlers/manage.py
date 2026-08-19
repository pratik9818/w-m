"""Owner control over an existing site: see its state, undo a change, delete it.

/undo is the escape hatch that was missing when a bad edit could rewrite a whole site:
it redeploys the previous version's stored bytes, so it costs no AI call and cannot
introduce anything new.
"""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from bot_api.services.business_service import get_business_by_id, list_businesses_for_owner
from bot_api.services.edit_ops import is_build_stale, is_business_busy
from bot_api.services.redis_client import get_redis
from bot_api.services.session import (
    clear_pending_edit,
    get_active_business_id,
    set_pending_edit,
)
from db.base import session_scope
from db.models import Business, SiteVersion
from worker.codegen.quota import KIND_LABELS, get_quota_summary

logger = logging.getLogger(__name__)
router = Router(name="manage")

STATUS_WORDS = {
    "none": "not built yet",
    "queued": "waiting to build",
    "generating": "being written",
    "testing": "being checked",
    "deploying": "being published",
    "live": "live",
    "failed": "last build failed",
}


async def _resolve_active(telegram_user_id: int) -> Business | None:
    """The site free-text edits currently apply to, or the only one they own."""
    active_id = await get_active_business_id(get_redis(), telegram_user_id)
    async with session_scope() as session:
        if active_id is not None:
            business = await get_business_by_id(session, active_id, telegram_user_id)
            if business is not None:
                return business
        owned = await list_businesses_for_owner(session, telegram_user_id)
        return owned[0] if len(owned) == 1 else None


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    async with session_scope() as session:
        businesses = await list_businesses_for_owner(session, message.from_user.id)
        if not businesses:
            await message.answer("You don't have any sites yet. Use /newsite to create one.")
            return

        blocks = []
        for business in businesses:
            version = (await session.execute(
                select(SiteVersion)
                .where(SiteVersion.business_id == business.id)
                .order_by(SiteVersion.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()

            state = STATUS_WORDS.get(business.generation_status, business.generation_status)
            if is_build_stale(business):
                state = "interrupted — send your change again"

            lines = [f"<b>{business.name}</b> — {state}"]
            if business.deployment_url:
                lines.append(business.deployment_url)
            if version is not None:
                lines.append(f"version {version.version_number}, {version.created_at:%d %b %H:%M} UTC")
                if version.status == "failed" and version.error:
                    reason = version.error.split(":", 1)[0]
                    lines.append(f"last build failed at the <i>{reason}</i> step")
            blocks.append("\n".join(lines))

    await message.answer("\n\n".join(blocks))


# Measured from real builds: a full site runs ~20-24k and a patched edit ~7-12k. Used only
# to translate the allowance into "how many more websites/changes", which is the only form
# of this number an owner can act on.
AVG_BUILD_COST = 22_000
AVG_EDIT_COST = 9_000


def _bar(percent: float, width: int = 10) -> str:
    filled = min(int(round(percent / 100 * width)), width)
    return "█" * filled + "░" * (width - filled)


@router.message(Command("token"))
async def cmd_token(message: Message) -> None:
    async with session_scope() as session:
        q = await get_quota_summary(session, message.from_user.id)

    if q["operations"] == 0:
        await message.answer(
            "You haven't used any of your allowance yet — enough for roughly "
            f"<b>{q['limit'] // AVG_BUILD_COST}</b> new websites, or many more small changes."
        )
        return

    # An owner has no idea what a "token" is. What they want to know is how much they can
    # still do, so the headline figure is in websites and changes, not raw numbers.
    builds_left = q["remaining"] // AVG_BUILD_COST
    edits_left = q["remaining"] // AVG_EDIT_COST

    lines = [
        "📊 <b>Your allowance</b>",
        "",
        f"{_bar(q['percent_used'])}  {q['percent_used']}% used",
        "",
        "With what's left you can make roughly:",
        f"  • <b>{builds_left}</b> more new websites, or",
        f"  • <b>{edits_left}</b> more changes to a site you already have",
        "",
        "Where it's gone so far:",
    ]
    for kind, tokens, count in q["by_kind"]:
        label = KIND_LABELS.get(kind, kind)
        share = round(tokens / q["used"] * 100) if q["used"] else 0
        lines.append(f"  • {label} — {count}x ({share}%)")

    lines += [
        "",
        f"<i>You've used {q['used']:,} of {q['limit']:,} — that's how it's measured behind "
        "the scenes. There's also a daily cap, so if a build won't start, waiting a while "
        "usually fixes it.</i>",
    ]

    await message.answer("\n".join(lines))


@router.message(Command("undo"))
async def cmd_undo(message: Message) -> None:
    business = await _resolve_active(message.from_user.id)
    if business is None:
        await message.answer("Which site? Use /mysites to pick one first.")
        return

    if is_business_busy(business):
        await message.answer(
            f"<b>{business.name}</b> is being updated right now — wait for that to finish first."
        )
        return

    async with session_scope() as session:
        previous = (await session.execute(
            select(SiteVersion)
            .where(
                SiteVersion.business_id == business.id,
                SiteVersion.status == "live",
                SiteVersion.files.isnot(None),
            )
            .order_by(SiteVersion.version_number.desc())
            .limit(2)
        )).scalars().all()

    if len(previous) < 2:
        await message.answer(
            f"There's no earlier version of <b>{business.name}</b> to go back to yet — "
            "I only have the version that's live now."
        )
        return

    target = previous[1]
    await set_pending_edit(
        get_redis(), business.id, {"operation": "undo", "version_id": str(target.id)}
    )
    await message.answer(
        f"This will put <b>{business.name}</b> back to version {target.version_number} "
        f"({target.created_at:%d %b %H:%M} UTC), exactly as it was.\n\n"
        "Note it restores the published pages, not your saved details — so if you ask me to "
        "rebuild the site later, the newer wording would come back.\n\n"
        'Reply "yes" to roll back.'
    )


@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    business = await _resolve_active(message.from_user.id)
    if business is None:
        await message.answer("Which site? Use /mysites to pick one first.")
        return

    await set_pending_edit(get_redis(), business.id, {"operation": "delete_site"})
    await message.answer(
        f"This permanently deletes <b>{business.name}</b> and takes "
        f"{business.deployment_url or 'its site'} offline. It cannot be undone.\n\n"
        'Reply "yes" to delete it.'
    )


async def cancel_pending(business_id) -> None:
    await clear_pending_edit(get_redis(), business_id)
