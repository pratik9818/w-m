"""Turning a Razorpay webhook into an entitlement, and a message the owner will understand.

Kept out of the route so it can be tested without standing up FastAPI, and because the
interesting decisions here are about money rather than about HTTP.

The webhook is the only thing that grants a plan. The browser also hands back a signed
confirmation when checkout succeeds, and that is used purely to show a success page
without making somebody stare at a spinner -- it never writes an entitlement. The
distinction matters because the browser is under the customer's control and the webhook is
not.

Every handler here is idempotent, enforced by a unique constraint rather than by care.
Razorpay retries a webhook it did not get a prompt 200 from, and it re-sends after its own
internal hiccups; `payments.razorpay_event_id` is unique, so a repeat insert raises and the
event is dropped before it can grant a second month.
"""
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.services.billing import owner_from_notes, unix_to_datetime
from bot_api.services.entitlements import _now
from bot_api.services.plans import get_plan
from db.models import Payment, Subscription

logger = logging.getLogger(__name__)

# How long a site keeps its paid features after a mandate fails. UPI mandates fail for
# dull reasons -- a bank outage, a day with no balance -- and taking somebody's custom
# domain away over a Tuesday is a good way never to be paid again. Razorpay retries within
# this window on its own.
GRACE_DAYS = 7

HANDLED = {
    "subscription.activated",
    "subscription.charged",
    "subscription.pending",
    "subscription.halted",
    "subscription.cancelled",
    "subscription.completed",
}


def _subscription_entity(payload: dict) -> dict:
    return ((payload.get("subscription") or {}).get("entity")) or {}


def _payment_entity(payload: dict) -> dict:
    return ((payload.get("payment") or {}).get("entity")) or {}


async def _record(
    session: AsyncSession, event_id: str, event: str, payload: dict, owner: int | None
) -> bool:
    """Write the event down. False if it has already been seen.

    The insert *is* the idempotency check -- there is no read-then-write, because two
    deliveries arriving at once would both read "not seen" and both proceed.
    """
    sub_entity = _subscription_entity(payload)
    pay_entity = _payment_entity(payload)
    session.add(Payment(
        razorpay_event_id=event_id,
        event=event,
        owner_telegram_id=owner,
        razorpay_subscription_id=sub_entity.get("id"),
        razorpay_payment_id=pay_entity.get("id"),
        amount_paise=int(pay_entity.get("amount") or 0),
        status=str(pay_entity.get("status") or sub_entity.get("status") or "unknown")[:20],
        payload=payload,
    ))
    try:
        await session.flush()
        return True
    except IntegrityError:
        await session.rollback()
        logger.info(
            "billing.duplicate_event",
            extra={"event": "billing.duplicate_event", "razorpay_event_id": event_id},
        )
        return False


async def _subscription_row(
    session: AsyncSession, owner: int, razorpay_subscription_id: str | None
) -> Subscription:
    sub = (await session.execute(
        select(Subscription).where(Subscription.owner_telegram_id == owner)
    )).scalar_one_or_none()
    if sub is None:
        sub = Subscription(owner_telegram_id=owner, plan="free", status="active")
        session.add(sub)
        await session.flush()
    if razorpay_subscription_id:
        sub.razorpay_subscription_id = razorpay_subscription_id
    return sub


async def handle_event(
    session: AsyncSession, event_id: str, event: str, payload: dict
) -> str | None:
    """Apply one webhook event. Returns the message to send the owner, if any.

    The message comes back rather than being sent here so that the database work commits
    before anything is promised in a chat -- an owner told their plan is active by a
    transaction that then rolls back has been lied to in the most expensive possible way.
    """
    entity = _subscription_entity(payload)
    owner = owner_from_notes(entity)

    if owner is None:
        # Nothing to credit. Still recorded, because a payment we cannot attribute is
        # exactly the thing somebody will need to find later.
        await _record(session, event_id, event, payload, None)
        await session.commit()
        logger.error(
            "billing.unattributable_event",
            extra={"event": "billing.unattributable_event", "razorpay_event": event,
                   "subscription_id": entity.get("id")},
        )
        return None

    if not await _record(session, event_id, event, payload, owner):
        return None

    if event not in HANDLED:
        await session.commit()
        return None

    sub = await _subscription_row(session, owner, entity.get("id"))
    notes = entity.get("notes") or {}
    plan = get_plan(notes.get("plan") or sub.plan)
    period = notes.get("period") or sub.period or "monthly"
    message: str | None = None

    if event in ("subscription.activated", "subscription.charged"):
        first_time = sub.plan != plan.code or sub.status != "active"
        sub.plan = plan.code
        sub.period = period
        sub.status = "active"
        sub.cancel_at_period_end = False
        sub.grace_until = None
        sub.razorpay_customer_id = entity.get("customer_id") or sub.razorpay_customer_id
        # These two are what roll the allowance: a new period_start opens a fresh
        # usage_periods row the next time anything is counted.
        sub.current_period_start = unix_to_datetime(entity.get("current_start")) or _now()
        sub.current_period_end = unix_to_datetime(entity.get("current_end"))

        renews = sub.current_period_end
        renews_line = f"\nYour next payment is on {renews:%-d %B}." if renews else ""
        if first_time:
            message = (
                f"✅ Payment received — you're on <b>{plan.name}</b>.\n\n"
                f"You now have <b>{plan.changes} changes</b> a month and "
                f"<b>{plan.sites} website{'s' if plan.sites > 1 else ''}</b>. "
                "The footer line is already gone from your site."
                f"{renews_line}\n\nSend /plan any time to see what's left."
            )
        else:
            message = (
                f"✅ ₹{plan.monthly_rupees if period == 'monthly' else plan.yearly_rupees} "
                f"received for this {'month' if period == 'monthly' else 'year'}. "
                f"Your {plan.changes} changes are back.{renews_line}"
            )

    elif event == "subscription.pending":
        # The mandate could not be debited yet but Razorpay has not given up. Nothing is
        # taken away here on purpose -- this fires for a bank being slow.
        sub.status = "pending"
        message = (
            "⚠️ This month's payment hasn't gone through yet — your bank hasn't approved it. "
            "Nothing has changed on your site and I'll keep trying for a few days."
        )

    elif event == "subscription.halted":
        sub.status = "halted"
        sub.grace_until = _now() + timedelta(days=GRACE_DAYS)
        message = (
            f"⚠️ Your payment failed and Razorpay has stopped retrying.\n\n"
            f"Your site stays live and your plan keeps working for <b>{GRACE_DAYS} more days</b>. "
            "Send /upgrade to set the payment up again — nothing is lost if you do it in time."
        )

    elif event == "subscription.cancelled":
        ends = sub.current_period_end
        if ends and ends > _now():
            sub.cancel_at_period_end = True
            message = (
                f"Your plan is cancelled and won't renew. You keep everything until "
                f"<b>{ends:%-d %B}</b>, then the account goes back to the free plan.\n\n"
                "Your website stays online either way."
            )
        else:
            sub.plan = "free"
            sub.status = "active"
            sub.current_period_start = None
            sub.current_period_end = None
            message = (
                "Your plan is cancelled and the account is back on the free plan. "
                "Your website stays online."
            )

    elif event == "subscription.completed":
        sub.plan = "free"
        sub.status = "active"
        sub.current_period_start = None
        sub.current_period_end = None
        message = (
            "Your subscription has run its course and the account is back on the free plan. "
            "Send /upgrade to start it again — your website stays online."
        )

    await session.commit()
    logger.info(
        "billing.event_applied",
        extra={"event": "billing.event_applied", "razorpay_event": event,
               "owner": owner, "plan": sub.plan, "status": sub.status},
    )
    return message
