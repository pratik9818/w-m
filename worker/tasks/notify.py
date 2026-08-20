from aiogram import Bot

from db.models import Business

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
    # The edit ran but changed nothing -- almost always because it was aimed at something
    # that isn't on the page. Asking for it in different words genuinely does help, so say
    # that rather than implying a fault the owner can do nothing about.
    "not_applied": (
        "I couldn't work out how to make that change to {name}, so nothing was altered — "
        "your site is exactly as it was. Could you tell me again in different words? "
        "It helps to say which part of the page you mean and what you'd like it to look like."
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
    bot: Bot, business: Business, usage: dict | None = None, remaining: int | None = None
) -> None:
    text = f"🎉 <b>{business.name}</b> is live! {business.deployment_url}"
    if usage and remaining is not None:
        # Show both halves: the raw cost of *this* build (asked for directly -- an owner
        # watching their allowance wants to see what each change actually spent) and the
        # same figure translated into remaining changes, which is the part they can act on.
        spent = usage["input_tokens"] + usage["output_tokens"]
        edits_left = remaining // 9_000
        text += (
            f"\n\n📊 This update used <b>{spent:,}</b> tokens."
            f"\n{remaining:,} left — room for about <b>{edits_left}</b> more changes."
            f"\nSend /token for details."
        )
    await bot.send_message(business.owner_telegram_id, text)


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


async def notify_owner_progress(bot: Bot, business: Business, stage: str) -> None:
    template = PROGRESS_COPY.get(stage)
    if template is None:
        return
    await bot.send_message(business.owner_telegram_id, template.format(name=business.name))
