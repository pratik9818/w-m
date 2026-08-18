from aiogram import Bot

from db.models import Business

PROGRESS_COPY = {
    "generating": "🔧 Building the new version of <b>{name}</b>...",
    "testing": "🧪 Running quality checks on <b>{name}</b>...",
    "deploying": "🚀 Publishing <b>{name}</b>...",
}

FAILURE_COPY = {
    "generation": (
        "Sorry — I ran into a problem generating {name}'s website and had to stop. "
        "Nothing was published. Please try again in a bit, or reach out if this keeps happening."
    ),
    "quota": (
        "You've hit the free generation limit for now, so I couldn't build {name}'s website. "
        "Nothing was published."
    ),
    "sandbox": (
        "I put together a first draft of {name}'s site, but it didn't pass my automated "
        "quality checks, so I didn't publish it. Nothing is live yet — try again in a bit."
    ),
    "deploy": (
        "{name}'s website is built and passed testing, but I couldn't get it published live "
        "due to a hosting error. Nothing is live yet — try again in a bit."
    ),
    "interrupted": (
        "Sorry — the update to {name}'s website was interrupted before it finished, so I've "
        "stopped it. Your site is still live and unchanged. Please send your change again."
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
        "Something went wrong while building {name}'s website and I had to stop. "
        "Nothing was published — please try again in a bit."
    ),
}


async def notify_owner_success(
    bot: Bot, business: Business, usage: dict | None = None, remaining: int | None = None
) -> None:
    text = f"🎉 <b>{business.name}</b> is live! {business.deployment_url}"
    if usage:
        spent = usage["input_tokens"] + usage["output_tokens"]
        text += f"\n\n📊 This update used <b>{spent:,}</b> tokens."
        if remaining is not None:
            text += f" You have <b>{remaining:,}</b> left — /quota for details."
    await bot.send_message(business.owner_telegram_id, text)


async def notify_owner_failure(bot: Bot, business: Business, stage: str) -> None:
    template = FAILURE_COPY.get(stage, FAILURE_COPY["unknown"])
    await bot.send_message(business.owner_telegram_id, template.format(name=business.name))


async def notify_owner_progress(bot: Bot, business: Business, stage: str) -> None:
    template = PROGRESS_COPY.get(stage)
    if template is None:
        return
    await bot.send_message(business.owner_telegram_id, template.format(name=business.name))
