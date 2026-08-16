from aiogram import Bot

from db.models import Business

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
    "unknown": (
        "Something went wrong while building {name}'s website and I had to stop. "
        "Nothing was published — please try again in a bit."
    ),
}


async def notify_owner_success(bot: Bot, business: Business) -> None:
    await bot.send_message(
        business.owner_telegram_id,
        f"🎉 <b>{business.name}</b> is live! {business.deployment_url}",
    )


async def notify_owner_failure(bot: Bot, business: Business, stage: str) -> None:
    template = FAILURE_COPY.get(stage, FAILURE_COPY["unknown"])
    await bot.send_message(business.owner_telegram_id, template.format(name=business.name))
