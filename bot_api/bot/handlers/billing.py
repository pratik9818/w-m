"""Everything the owner sees about money: /upgrade, /plan, and cancelling.

The shape of the flow is deliberate. Tapping a plan does not open a wall of text, it opens
a payment page -- one screen, one price, one button. Everything that has to be said about
what the plan includes is said on that page, where somebody is actually deciding, rather
than in a chat message they scroll past on the way to the button.

Nothing here grants anything. The confirmation an owner receives is sent by the webhook
handler after Razorpay has confirmed the money moved, which is why there is no "thanks for
paying" message in this file at all.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot_api.config import get_settings
from bot_api.services.billing import (
    BillingCallFailed,
    BillingNotConfigured,
    cancel_subscription,
    is_configured,
    open_checkout_session,
)
from bot_api.services.entitlements import load
from bot_api.services.plans import (
    PAID_PLANS,
    Plan,
    get_plan,
    price_rupees,
)
from bot_api.services.redis_client import get_redis
from db.base import session_scope

logger = logging.getLogger(__name__)

router = Router(name="billing")


def _bar(used: int, included: int, width: int = 10) -> str:
    if included <= 0:
        return "░" * width
    filled = min(int(round(used / included * width)), width)
    return "█" * filled + "░" * (width - filled)


def plans_keyboard(period: str = "monthly") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in PAID_PLANS:
        builder.button(
            text=f"{plan.name} — ₹{price_rupees(plan, period):,}/{'mo' if period == 'monthly' else 'yr'}",
            callback_data=f"billing:pick:{plan.code}:{period}",
        )
    if period == "monthly":
        builder.button(text="📅 Pay yearly — 2 months free", callback_data="billing:period:yearly")
    else:
        builder.button(text="↩ Back to monthly", callback_data="billing:period:monthly")
    builder.adjust(1)
    return builder.as_markup()


def pay_keyboard(url: str, rupees: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💳 Pay ₹{rupees:,}", url=url)
    builder.adjust(1)
    return builder.as_markup()


def _plans_message(current_plan_name: str, changes_left: int, period: str) -> str:
    lines = [f"You're on <b>{current_plan_name}</b> — {changes_left} changes left.", ""]
    for plan in PAID_PLANS:
        rupees = price_rupees(plan, period)
        unit = "a month" if period == "monthly" else "a year"
        lines.append(f"<b>{plan.name}</b> — ₹{rupees:,} {unit}")
        lines.append(
            f"   {plan.sites} website{'s' if plan.sites > 1 else ''}, "
            f"{plan.changes} changes a month"
        )
        lines.append("")
    lines.append("<i>Colour and font tweaks are free on every plan, including the free one.</i>")
    return "\n".join(lines)


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message) -> None:
    async with session_scope() as session:
        ent = await load(session, message.from_user.id)

    if not is_configured():
        await message.answer(
            "Paid plans aren't switched on yet. Everything you've built stays exactly as it "
            "is — I'll let you know the moment they open."
        )
        return

    await message.answer(
        _plans_message(ent.plan.name, ent.changes_left, "monthly"),
        reply_markup=plans_keyboard("monthly"),
    )


@router.callback_query(F.data.startswith("billing:period:"))
async def on_period_toggle(callback: CallbackQuery) -> None:
    period = callback.data.split(":")[2]
    async with session_scope() as session:
        ent = await load(session, callback.from_user.id)
    await callback.message.edit_text(
        _plans_message(ent.plan.name, ent.changes_left, period),
        reply_markup=plans_keyboard(period),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("billing:pick:"))
async def on_plan_picked(callback: CallbackQuery) -> None:
    _, _, plan_code, period = callback.data.split(":")
    plan: Plan = get_plan(plan_code)
    settings = get_settings()

    # The standalone site in web/ when it is deployed, this app's own pages otherwise.
    # Either way the path is /pay/<token>, so the link a customer sees is the same shape
    # and moving between the two never invalidates a link already sent into a chat.
    checkout_base = (settings.checkout_site_url or settings.public_base_url).rstrip("/")

    if not checkout_base:
        # Neither the standalone site nor this app has a public address configured, so
        # there is nowhere to send anybody. A broken link is worse than an honest refusal.
        logger.error("billing.no_checkout_base_url")
        await callback.answer()
        await callback.message.answer(
            "I can't open the payment page just now. Try again in a few minutes."
        )
        return

    await callback.answer()
    try:
        async with session_scope() as session:
            ent = await load(session, callback.from_user.id)
        token, payload = await open_checkout_session(
            get_redis(), plan, period, callback.from_user.id, callback.from_user.username
        )
    except BillingNotConfigured:
        logger.exception("billing not configured while a customer was trying to pay")
        await callback.message.answer(
            "Paid plans aren't quite ready yet — nothing has been charged. I'll let you know."
        )
        return
    except BillingCallFailed:
        logger.exception("razorpay refused to open a subscription")
        await callback.message.answer(
            "I couldn't reach the payment provider just now. Nothing has been charged — "
            "please try /upgrade again in a minute."
        )
        return

    rupees = payload["amount_paise"] // 100
    unit = "a month" if period == "monthly" else "a year"
    url = f"{checkout_base}/pay/{token}"

    await callback.message.answer(
        f"<b>{plan.name}</b> — ₹{rupees:,} {unit}\n\n"
        f"Tap below to pay by UPI, card or net banking. It takes about a minute, and the "
        f"link works for the next hour.\n\n"
        f"<i>You're currently on {ent.plan.name}. Nothing changes until the payment goes through.</i>",
        reply_markup=pay_keyboard(url, rupees),
    )


@router.message(Command("plan"))
async def cmd_plan(message: Message) -> None:
    async with session_scope() as session:
        ent = await load(session, message.from_user.id)

    plan = ent.plan
    included = ent.period.changes_included + ent.subscription.topup_changes
    used = min(ent.period.changes_used, included)

    lines = [f"📦 <b>{plan.name}</b>"]
    if plan.recurring:
        rupees = price_rupees(plan, ent.subscription.period)
        unit = "month" if ent.subscription.period == "monthly" else "year"
        lines[0] += f" — ₹{rupees:,} a {unit}"
    lines.append("")
    lines.append(f"{_bar(used, included)}  {used} of {included} changes used")

    if plan.recurring and ent.renews_on:
        if ent.subscription.cancel_at_period_end:
            lines.append(f"Ends on {ent.renews_on:%-d %B} — it won't renew.")
        elif ent.subscription.status == "halted":
            lines.append("⚠️ Your last payment failed. Send /upgrade to set it up again.")
        else:
            lines.append(f"Renews on {ent.renews_on:%-d %B}.")
    elif not plan.recurring:
        lines.append(f"That's {plan.changes} changes in total, not per month.")

    lines.append(f"Websites: {ent.sites_used} of {plan.sites}.")
    if ent.subscription.topup_changes:
        lines.append(f"Includes {ent.subscription.topup_changes} top-up changes you bought.")
    lines.append("")
    lines.append("<i>Colour and font tweaks don't count against this — they're always free.</i>")

    builder = InlineKeyboardBuilder()
    if plan.code == "free":
        builder.button(text="⬆️ See paid plans", callback_data="billing:show")
    else:
        if plan.code != "business":
            builder.button(text="⬆️ Move up a plan", callback_data="billing:show")
        if not ent.subscription.cancel_at_period_end:
            builder.button(text="✖ Cancel my plan", callback_data="billing:cancel")
    builder.adjust(1)

    await message.answer("\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(F.data == "billing:show")
async def on_show_plans(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        ent = await load(session, callback.from_user.id)
    await callback.answer()
    await callback.message.answer(
        _plans_message(ent.plan.name, ent.changes_left, "monthly"),
        reply_markup=plans_keyboard("monthly"),
    )


@router.callback_query(F.data == "billing:cancel")
async def on_cancel_request(callback: CallbackQuery) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text="Yes, cancel it", callback_data="billing:cancel:confirm")
    builder.button(text="No, keep my plan", callback_data="billing:cancel:keep")
    builder.adjust(1)
    await callback.answer()
    await callback.message.answer(
        "Cancelling stops the next payment. You keep everything you're paying for until the "
        "end of the period you've already paid for, and <b>your website stays online either "
        "way</b> — you just won't be able to make new changes once it runs out.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "billing:cancel:keep")
async def on_cancel_keep(callback: CallbackQuery) -> None:
    await callback.answer("Nothing changed.")
    await callback.message.edit_text("No problem — your plan carries on as it is.")


@router.callback_query(F.data == "billing:cancel:confirm")
async def on_cancel_confirm(callback: CallbackQuery) -> None:
    async with session_scope() as session:
        ent = await load(session, callback.from_user.id)
        subscription_id = ent.subscription.razorpay_subscription_id

    await callback.answer()
    if not subscription_id:
        await callback.message.answer("You're not on a paid plan, so there's nothing to cancel.")
        return

    try:
        await cancel_subscription(subscription_id, at_cycle_end=True)
    except (BillingNotConfigured, BillingCallFailed):
        logger.exception("failed to cancel subscription %s", subscription_id)
        await callback.message.answer(
            "I couldn't reach the payment provider to cancel it. Nothing has changed — "
            "please try again in a few minutes."
        )
        return

    # The subscription row is not edited here on purpose. Razorpay will send
    # `subscription.cancelled`, and letting that one path write the change means the bot
    # and the payment provider cannot end up disagreeing about what happened.
    await callback.message.answer(
        "Done — your plan won't renew. You keep everything until the end of the period "
        "you've paid for, and I'll confirm here once the provider has processed it."
    )


def out_of_changes_keyboard() -> InlineKeyboardMarkup:
    """Offered at the moment somebody runs out, which is when the offer actually lands.

    Only the plans, for now. Top-up packs are priced in the catalogue but not sold yet --
    they are a one-off Razorpay order rather than a subscription, which is a second
    checkout flow, and shipping a button that leads nowhere is worse than not having one.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="⬆️ See plans", callback_data="billing:show")
    builder.adjust(1)
    return builder.as_markup()
