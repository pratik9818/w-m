"""Site creation from a single free-text brief.

This replaced a ~12-question flow. The old one collected the same fields more precisely,
but it was tiring enough that an owner typed "So not include this" into the hours question
purely to get past it -- and that phrase was then published on their live site as their
opening hours. One question that the model interprets is both faster and, in practice,
produced cleaner data.

The model decides category, theme, layout and the marketing copy; it only comes back with
a question when it genuinely cannot tell what the business is.
"""
import logging
import uuid

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot_api.bot.filters import has_text
from bot_api.bot.states.onboarding import OnboardingStates
from bot_api.services.business_service import OnboardingSpec, create_business_from_spec
from bot_api.services.onboarding_ai import BriefParseFailed, parse_business_brief
from bot_api.services.queue import enqueue_generation
from bot_api.services.redis_client import get_redis
from bot_api.services.session import set_active_business
from bot_api.services.validation import EMAIL_RE, FIELD_LIMITS, THEMES
from bot_api.services.quota_helpers import record_parse_usage
from db.base import session_scope

logger = logging.getLogger(__name__)
router = Router(name="onboarding")

PROMPT = (
    "Tell me about the website you want — in your own words, all in one message.\n\n"
    "For example:\n"
    "<i>\"A landing page for my tattoo studio Inkwell in Leeds. We do custom designs, "
    "cover-ups and piercings. Call us on 0113 496 0000.\"</i>\n\n"
    "Include anything you want on the site — what you do, what you offer, and your phone, "
    "email or opening hours if you'd like them shown. I'll write the rest."
)

MAX_BRIEF_CHARS = 2000
MAX_SERVICES = 15


async def begin_onboarding(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(business_id=str(uuid.uuid4()), brief_history=[])
    await state.set_state(OnboardingStates.waiting_brief)
    await message.answer(PROMPT)


def _clip(value, limit_key: str) -> str | None:
    """Trim to the column limit rather than rejecting -- the owner never saw a field here,
    so bouncing their whole description over a long sentence would be baffling."""
    if not value or not str(value).strip():
        return None
    return str(value).strip()[: FIELD_LIMITS[limit_key]]


def _spec_from_operation(business_id: str, op: dict) -> OnboardingSpec:
    spec = OnboardingSpec()
    spec.business_id = uuid.UUID(business_id)
    spec.name = _clip(op.get("name"), "name") or "My business"
    spec.category = (str(op.get("category") or "Other").strip())[:40]
    spec.tagline = _clip(op.get("tagline"), "tagline")
    spec.about = _clip(op.get("about"), "about")
    spec.phone = _clip(op.get("phone"), "phone")
    spec.address = _clip(op.get("address"), "address")
    spec.hours_display_text = _clip(op.get("hours"), "hours")

    email = _clip(op.get("email"), "email")
    # A malformed address would fail validation later; better no email than a broken one.
    spec.email = email if email and EMAIL_RE.match(email) else None

    theme = str(op.get("theme") or "").strip().lower()
    spec.theme = theme if theme in THEMES else "classic"
    spec.layout = "landing" if str(op.get("layout", "")).lower().startswith("land") else "multipage"

    services = []
    for item in (op.get("services") or [])[:MAX_SERVICES]:
        if isinstance(item, dict) and item.get("name"):
            services.append({
                "name": _clip(item["name"], "service_name"),
                "price_label": _clip(item.get("price_label"), "service_price_label"),
            })
    spec.services = services
    return spec


@router.message(OnboardingStates.waiting_brief, Command("cancel"))
async def on_brief_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Send /newsite whenever you're ready.")


@router.message(OnboardingStates.waiting_brief, has_text)
async def on_brief(message: Message, state: FSMContext) -> None:
    brief = message.text.strip()[:MAX_BRIEF_CHARS]
    data = await state.get_data()
    history = data.get("brief_history", [])

    await message.answer("🧠 Reading that and putting your site together...")
    try:
        op, usage = await parse_business_brief(brief, history)
    except BriefParseFailed:
        logger.exception("brief parsing failed")
        await message.answer("Sorry, I couldn't process that just now — please try again in a moment.")
        return

    await record_parse_usage(message.from_user.id, usage)

    if op["operation"] == "need_more_info":
        # Keep everything they've said so far; the next message is added to this brief
        # rather than replacing it.
        await state.update_data(brief_history=history + [brief])
        await message.answer(op.get("question") or "Could you tell me a bit more about your business?")
        return

    spec = _spec_from_operation(data["business_id"], op)
    async with session_scope() as session:
        business = await create_business_from_spec(session, message.from_user.id, spec)

    await set_active_business(get_redis(), message.from_user.id, business.id)
    await enqueue_generation(business.id, trigger="create")
    await state.clear()

    shape = "one-page landing site" if spec.layout == "landing" else "four-page site"
    extras = []
    if spec.services:
        extras.append(f"{len(spec.services)} service(s)")
    if spec.phone or spec.email:
        extras.append("your contact details")
    detail = f" including {' and '.join(extras)}" if extras else ""

    await message.answer(
        f"🎉 Building <b>{business.name}</b> — a {shape}{detail}.\n\n"
        "I'll message you here with the live link as soon as it's ready. "
        "After that, just tell me anything you want changed."
    )
