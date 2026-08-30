"""Whether this owner is allowed to do this, and what it uses up.

Every quota decision in the bot goes through this module, and it is called from exactly two
places: `catch_all_edit` in the edit handler, and `on_confirm` in onboarding. Keeping it to
two call sites is deliberate -- quota logic that leaks across a codebase is how a paid
customer ends up blocked by a check nobody remembered writing.

The order of operations matters more than it looks. The check runs *before* the message is
read, not after: reading a message costs about ₹2, and paying that to tell somebody they
have run out is a small bill that is also insulting. But the *charge* happens after the
edit succeeds, once we know what it actually was -- so a question costs nothing, a colour
change costs nothing, and a failed edit costs nothing. Reserve the minimum up front,
settle the real amount at the end.

Three ceilings, doing different jobs:

  - **Changes** is the one the owner sees and the one they are sold.
  - **Tokens** is a circuit breaker set far above honest maximum use. The change counter
    treats every change as equal; a change genuinely ranges from ₹1.60 to ₹40, and this is
    what catches the tail.
  - **Changes per day** is a rate limit, not a budget, so it lives in Redis rather than
    Postgres. It stops a runaway loop and keeps republishes inside Cloudflare Pages'
    account-wide monthly deploy budget, which every change consumes one of.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.services.plans import (
    CHANGES_PER_DAY,
    QUESTIONS_PER_DAY,
    Plan,
    get_plan,
)
from db.models import Business, Subscription, UsagePeriod

logger = logging.getLogger(__name__)


class QuotaBlocked(Exception):
    """Base for every "no, and here is why" outcome.

    Carries owner-facing text because the handler that catches it has no better idea what
    to say than the check that raised it, and duplicating the copy at each call site is
    how the free tier ended up described three different ways.
    """

    def __init__(self, message: str, offer_upgrade: bool = True) -> None:
        self.owner_message = message
        self.offer_upgrade = offer_upgrade
        super().__init__(message)


class NoChangesLeft(QuotaBlocked):
    pass


class SiteLimitReached(QuotaBlocked):
    pass


class TokenCeilingHit(QuotaBlocked):
    pass


class DailyCapHit(QuotaBlocked):
    pass


@dataclass
class Entitlement:
    plan: Plan
    subscription: Subscription
    period: UsagePeriod
    sites_used: int

    @property
    def changes_left(self) -> int:
        allowed = self.period.changes_included + self.subscription.topup_changes
        return max(allowed - self.period.changes_used, 0)

    @property
    def sites_left(self) -> int:
        return max(self.plan.sites - self.sites_used, 0)

    @property
    def renews_on(self) -> datetime | None:
        return self.subscription.current_period_end


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _subscription_for(session: AsyncSession, owner_telegram_id: int) -> Subscription:
    """The owner's subscription row, created on the free plan if they have never had one.

    Everyone has a subscription, including people who have never paid. The alternative --
    a null meaning "free" -- puts a branch in every caller and gets one of them wrong.
    """
    sub = (await session.execute(
        select(Subscription).where(Subscription.owner_telegram_id == owner_telegram_id)
    )).scalar_one_or_none()
    if sub is not None:
        return await _expire_if_lapsed(session, sub)

    sub = Subscription(owner_telegram_id=owner_telegram_id, plan="free", status="active")
    session.add(sub)
    try:
        await session.flush()
    except IntegrityError:
        # Two messages from the same owner arriving together. The unique constraint on
        # owner_telegram_id is what makes this safe; the loser just re-reads.
        await session.rollback()
        sub = (await session.execute(
            select(Subscription).where(Subscription.owner_telegram_id == owner_telegram_id)
        )).scalar_one()
    return sub


async def _expire_if_lapsed(session: AsyncSession, sub: Subscription) -> Subscription:
    """Drop a subscription to free once it has genuinely run out.

    Two ways that happens: somebody cancelled and their paid period has now ended, or a
    mandate failed and the grace period is over. Both are checked here, on read, rather
    than by a scheduled job -- there is no scheduler in this codebase, and a downgrade
    nobody is around to notice does not need to happen at midnight sharp.
    """
    if sub.plan == "free":
        return sub

    now = _now()
    lapsed = (
        (sub.cancel_at_period_end and sub.current_period_end and sub.current_period_end <= now)
        or (sub.status == "halted" and sub.grace_until and sub.grace_until <= now)
    )
    if not lapsed:
        return sub

    logger.info(
        "billing.downgraded_to_free",
        extra={"event": "billing.downgraded_to_free", "owner": sub.owner_telegram_id,
               "from_plan": sub.plan, "reason": "cancelled" if sub.cancel_at_period_end else "halted"},
    )
    sub.plan = "free"
    sub.status = "active"
    sub.period = "monthly"
    sub.cancel_at_period_end = False
    sub.grace_until = None
    sub.current_period_start = None
    sub.current_period_end = None
    await session.flush()
    return sub


async def _period_for(
    session: AsyncSession, sub: Subscription, plan: Plan
) -> UsagePeriod:
    """The usage row currently being counted into, opening a new one when the month rolls.

    On a paid plan the window is Razorpay's billing cycle, so an owner's allowance resets
    on the day they actually pay rather than on the first of the month. On free it is a
    single row with no end: five changes is a lifetime total, and a free tier that quietly
    refills every month is not a free tier, it is an unpaid plan.
    """
    if plan.recurring and sub.current_period_start is not None:
        start = sub.current_period_start
        end = sub.current_period_end
    else:
        start = sub.created_at or _now()
        end = None

    period = (await session.execute(
        select(UsagePeriod).where(
            UsagePeriod.owner_telegram_id == sub.owner_telegram_id,
            UsagePeriod.period_start == start,
        )
    )).scalar_one_or_none()
    if period is not None:
        # An upgrade mid-period raises the allowance immediately -- somebody who pays more
        # today should not wait until next month to get what they paid for.
        if period.changes_included < plan.changes or period.plan != plan.code:
            period.changes_included = max(period.changes_included, plan.changes)
            period.plan = plan.code
            await session.flush()
        return period

    period = UsagePeriod(
        owner_telegram_id=sub.owner_telegram_id,
        plan=plan.code,
        period_start=start,
        period_end=end,
        changes_included=plan.changes,
    )
    session.add(period)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        period = (await session.execute(
            select(UsagePeriod).where(
                UsagePeriod.owner_telegram_id == sub.owner_telegram_id,
                UsagePeriod.period_start == start,
            )
        )).scalar_one()
    return period


async def load(session: AsyncSession, owner_telegram_id: int) -> Entitlement:
    """Everything the bot needs to know about what this owner may do, in one place."""
    sub = await _subscription_for(session, owner_telegram_id)
    plan = get_plan(sub.plan)
    period = await _period_for(session, sub, plan)
    sites_used = int((await session.execute(
        select(func.count(Business.id)).where(Business.owner_telegram_id == owner_telegram_id)
    )).scalar_one())
    return Entitlement(plan=plan, subscription=sub, period=period, sites_used=sites_used)


# ---------------------------------------------------------------- daily rate limits


def _day_key(kind: str, owner_telegram_id: int) -> str:
    return f"ratelimit:{kind}:{owner_telegram_id}:{_now():%Y-%m-%d}"


async def _bump_daily(redis, kind: str, owner_telegram_id: int, ceiling: int) -> bool:
    """Count one action against today's ceiling. False when the ceiling is already reached.

    Never blocks the owner on a Redis failure. A rate limiter that takes the product down
    when the cache blinks has made things worse than the abuse it was guarding against.
    """
    key = _day_key(kind, owner_telegram_id)
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 60 * 60 * 36)
        return count <= ceiling
    except Exception:
        logger.warning("ratelimit unavailable for %s/%s", kind, owner_telegram_id, exc_info=True)
        return True


async def check_question_allowed(redis, owner_telegram_id: int) -> None:
    if not await _bump_daily(redis, "question", owner_telegram_id, QUESTIONS_PER_DAY):
        raise DailyCapHit(
            f"You've asked me {QUESTIONS_PER_DAY} questions today, which is my daily limit. "
            "It resets tomorrow morning — your site is completely unaffected.",
            offer_upgrade=False,
        )


# ---------------------------------------------------------------- the gates


async def check_change_allowed(
    session: AsyncSession, redis, owner_telegram_id: int
) -> Entitlement:
    """Called before the message is read. Raises rather than returning a verdict.

    Reserves nothing: the allowance is settled by `consume` after the edit succeeds. What
    this guarantees is only that there is at least one change available to spend, which is
    the cheapest possible thing to know before paying to read a message.
    """
    ent = await load(session, owner_telegram_id)

    if ent.changes_left <= 0:
        if ent.plan.code == "free":
            raise NoChangesLeft(
                f"You've used all {ent.plan.changes} free changes. Your site stays live and "
                "keeps working exactly as it is — you just can't make new changes until you "
                "move to a paid plan.\n\nColour and font tweaks are still free, always."
            )
        when = ent.renews_on
        when_text = f" They come back on {when:%-d %B}." if when else ""
        raise NoChangesLeft(
            f"You've used all {ent.period.changes_included} changes on {ent.plan.name} this "
            f"month.{when_text}\n\nYour site stays live, and colour and font tweaks are still free."
        )

    if ent.period.tokens_used >= ent.plan.token_ceiling:
        # Deliberately not phrased as a limit the owner can plan around: it exists for the
        # rare account that is costing far more than its changes suggest, and explaining
        # the mechanism invites gaming it.
        raise TokenCeilingHit(
            "This account has done an unusual amount of work this month, so I've paused new "
            "changes on it. Your site stays live. Send me a message and I'll sort it out.",
            offer_upgrade=False,
        )

    if not await _bump_daily(redis, "change", owner_telegram_id, CHANGES_PER_DAY):
        raise DailyCapHit(
            f"That's {CHANGES_PER_DAY} changes today, which is my daily limit — it keeps one "
            "busy afternoon from eating a whole month's allowance. It resets tomorrow, and "
            "nothing you've already asked for is lost.",
            offer_upgrade=False,
        )

    return ent


async def check_new_site_allowed(session: AsyncSession, owner_telegram_id: int) -> Entitlement:
    ent = await load(session, owner_telegram_id)
    if ent.sites_left <= 0:
        if ent.plan.code == "free":
            raise SiteLimitReached(
                "The free plan covers one website, and you already have it. A paid plan lets "
                "you build more — and everything you've already made stays exactly as it is."
            )
        raise SiteLimitReached(
            f"{ent.plan.name} covers {ent.plan.sites} websites and you're using all of them. "
            "You can delete one you no longer need, or move up a plan."
        )
    return ent


async def consume(
    session: AsyncSession, owner_telegram_id: int, weight: int, tokens: int = 0
) -> None:
    """Settle up after an edit has actually been applied.

    Called with the real weight, which may be zero -- a question, a colour change, an edit
    that turned out to be a no-op. Tokens are recorded even when the weight is zero,
    because the circuit breaker's whole job is to see the work that the change counter
    cannot.
    """
    ent = await load(session, owner_telegram_id)
    if weight > 0:
        # Top-ups are spent first, so a month's included allowance is never stranded
        # behind changes somebody paid extra for.
        from_topup = min(weight, ent.subscription.topup_changes)
        if from_topup:
            ent.subscription.topup_changes -= from_topup
        ent.period.changes_used += weight - from_topup
    if tokens:
        ent.period.tokens_used += tokens
    await session.commit()


async def grant_topup(session: AsyncSession, owner_telegram_id: int, changes: int) -> None:
    sub = await _subscription_for(session, owner_telegram_id)
    sub.topup_changes += changes
    await session.commit()
