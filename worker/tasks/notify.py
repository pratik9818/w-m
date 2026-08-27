import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile

from bot_api.services.redis_client import get_redis
from bot_api.services.session import push_edit_turn
from db.models import Business
from worker.codegen.quota import AVG_EDIT_COST

logger = logging.getLogger(__name__)

# What the owner sees is one conversation, but it has two mouths: the bot process answers
# their messages, and the worker sends the results minutes later. Only the first half was
# ever written to the conversation buffer, so everything the worker said was invisible to
# the next message -- the bot could not remember its own results, its own failures, or its
# own suggestions. That is the whole of the "it forgets what it just told me" complaint.
_MEMORY_MAX_CHARS = 400


async def _remember(business: Business, text: str) -> None:
    """Record something the worker told the owner, so the next message is read against it.

    Never raises. A message that reached Telegram but could not be written to Redis is a
    worse memory, not a failed build -- and this runs at the very end of a pipeline that
    has already succeeded.
    """
    try:
        await push_edit_turn(
            get_redis(), business.id, "(waiting for their site)",
            {"bot_said": text[:_MEMORY_MAX_CHARS]},
        )
    except Exception:
        logger.warning("could not record what was said to the owner", exc_info=True)

# Telegram rejects a caption over 1024 characters outright.
CAPTION_MAX_LEN = 1024

# Written for a shop owner, not an engineer: they should never have to wonder what a word
# like "sandbox", "deploy" or "quality checks" means, or whether their live site is at risk.
PROGRESS_COPY = {
    "generating": "✍️ Writing <b>{name}</b>...",
    "testing": "🔍 Checking every page looks right...",
    "deploying": "🚀 Putting <b>{name}</b> online...",
}

FAILURE_COPY = {
    "generation": (
        "Sorry — I hit a problem while writing {name} and had to stop. "
        "Nothing has changed on your site. Please try again in a few minutes."
    ),
    "quota": (
        "You've used up your allowance for now, so I couldn't build {name}. "
        "Nothing has changed on your site — send /token to see where it went."
    ),
    "sandbox": (
        "I wrote a new version of {name}, but spotted a problem with it when I checked it "
        "over, so I haven't put it online. Your current site is untouched — try again, or "
        "tell me what you wanted in different words."
    ),
    "deploy": (
        "{name} is written and looks good, but I couldn't get it online just now. "
        "Your current site is untouched — please try again in a few minutes."
    ),
    # The edit ran and changed nothing. The old copy here said "I couldn't work out how to
    # make that change", which was untrue in the common case and cost a real owner two
    # days: their site already looked exactly as they were describing, and being told six
    # times that it could not be done sent them round the same loop with the same words.
    # Say what actually happened, and give them the two ways forward.
    # Not a failure at all: the site already says what was asked for. Kept separate
    # from "not_applied" because the owner needs the actual value to argue with -- being
    # told "already done" without being told *what* is already done is what turned one
    # misunderstanding into six identical messages.
    "already_set": (
        "That's already how <b>{name}</b> is set, so there was nothing to change and I "
        "haven't touched your site."
    ),
    "not_applied": (
        "I compared {name} against what you asked for and there was nothing left to "
        "change — it already looks that way, so I've left your site exactly as it is "
        "and published nothing.\n\n"
        "If you wanted it to go <b>further</b>, tell me roughly how much — \"twice as "
        "big\", \"fill the whole screen\", \"much darker\" — and I'll push it past "
        "where it is now.\n\n"
        "If you meant a different part of the page, tell me a word you can actually see "
        "on that part and I'll find it."
    ),
    "interrupted": (
        "Sorry — the update to {name} stopped partway through. Your site is still live and "
        "unchanged. Please send your change again."
    ),
    # Distinct from "quota" above: that one is the owner's own token budget, this one is
    # the AI provider's daily request cap hitting everyone at once. Conflating them told
    # a real owner their site "had a problem" when it just needed to wait for a reset.
    "daily_limit": (
        "I've hit today's limit for building websites, so I couldn't update {name} just now. "
        "Nothing was published and your site is still live. This resets automatically — "
        "please try again later."
    ),
    "unknown": (
        "Something went wrong while building {name} and I had to stop. "
        "Nothing has changed on your site — please try again in a few minutes."
    ),
}


async def notify_owner_success(
    bot: Bot,
    business: Business,
    usage: dict | None = None,
    remaining: int | None = None,
    screenshot: bytes | None = None,
    parse_tokens: int = 0,
) -> None:
    text = f"🎉 <b>{business.name}</b> is live! {business.deployment_url}"
    if usage and remaining is not None:
        # Show both halves: the raw cost of *this* build (asked for directly -- an owner
        # watching their allowance wants to see what each change actually spent) and the
        # same figure translated into remaining changes, which is the part they can act on.
        built = usage["input_tokens"] + usage["output_tokens"]
        spent = built + parse_tokens
        edits_left = remaining // AVG_EDIT_COST
        text += f"\n\n📊 This update used <b>{spent:,}</b> tokens."
        if parse_tokens and not built:
            # Worth saying, because it is the difference between this route and the old
            # one -- but the total above is the number they are actually charged.
            text += " Reading your message was the whole cost: the change itself was made directly, with no rebuild."
        text += (
            f"\n{remaining:,} left — room for about <b>{edits_left}</b> more changes."
            f"\nSend /token for details."
        )
    # A link is not evidence. An owner who cannot see what changed assumes nothing did --
    # that is how six identical requests happened -- so the new version is shown, not
    # described. Falls back to the plain message if Telegram rejects the image.
    if screenshot and len(text) <= CAPTION_MAX_LEN:
        try:
            await bot.send_photo(
                business.owner_telegram_id,
                BufferedInputFile(screenshot, filename="your-site.png"),
                caption=text,
            )
            await _remember(business, text)
            return
        except Exception:
            logger.warning("could not send the preview image, sending text instead", exc_info=True)

    await bot.send_message(business.owner_telegram_id, text)
    await _remember(business, text)


async def notify_owner_failure(
    bot: Bot, business: Business, stage: str, detail: str | None = None
) -> None:
    template = FAILURE_COPY.get(stage, FAILURE_COPY["unknown"])
    text = template.format(name=business.name)
    if detail:
        # "didn't pass my quality checks" is true and useless. Naming the actual defect
        # lets an owner judge whether to retry or change something.
        text += f"\n\nWhat went wrong: {detail}"
    await bot.send_message(business.owner_telegram_id, text)
    # Failures matter here more than successes. "You've used up your allowance" is the
    # message an owner replies to, and a reply read without it is read against nothing.
    await _remember(business, text)


async def notify_owner_progress(bot: Bot, business: Business, stage: str) -> None:
    template = PROGRESS_COPY.get(stage)
    if template is None:
        return
    await bot.send_message(business.owner_telegram_id, template.format(name=business.name))
