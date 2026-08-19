import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TokenUsage

FREE_TIER_TOKEN_LIMIT = 1_000_000


class QuotaExceeded(Exception):
    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(f"Token quota exceeded: {used}/{limit} tokens used.")


async def get_tokens_used(session: AsyncSession, owner_telegram_id: int) -> int:
    result = await session.execute(
        select(func.coalesce(func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens), 0)).where(
            TokenUsage.owner_telegram_id == owner_telegram_id
        )
    )
    return int(result.scalar_one())


async def check_quota(session: AsyncSession, owner_telegram_id: int) -> None:
    used = await get_tokens_used(session, owner_telegram_id)
    if used >= FREE_TIER_TOKEN_LIMIT:
        raise QuotaExceeded(used, FREE_TIER_TOKEN_LIMIT)


async def record_usage(
    session: AsyncSession,
    owner_telegram_id: int,
    business_id: uuid.UUID | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    kind: str = "create",
    requests: int = 1,
) -> None:
    session.add(
        TokenUsage(
            owner_telegram_id=owner_telegram_id,
            business_id=business_id,
            model=model,
            kind=kind,
            requests=requests,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )
    await session.commit()


KIND_LABELS = {
    "create": "building new sites",
    "rebuild": "rebuilding sites from scratch",
    "edit": "making changes",
    "parse": "understanding your messages",
    "repair": "fixing small problems before publishing",
}


async def get_quota_summary(session: AsyncSession, owner_telegram_id: int) -> dict:
    """Everything /token needs, in one round trip per figure.

    Reports requests alongside tokens deliberately: tokens are the budget we set, but the
    provider caps *requests* per day, so a report showing only tokens can say "plenty
    left" at the exact moment builds start being refused.
    """
    totals = (await session.execute(
        select(
            func.coalesce(func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens), 0),
            func.coalesce(func.sum(TokenUsage.requests), 0),
            func.count(TokenUsage.id),
        ).where(TokenUsage.owner_telegram_id == owner_telegram_id)
    )).one()

    by_kind = (await session.execute(
        select(
            TokenUsage.kind,
            func.coalesce(func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens), 0),
            func.count(TokenUsage.id),
        )
        .where(TokenUsage.owner_telegram_id == owner_telegram_id)
        .group_by(TokenUsage.kind)
        .order_by(func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens).desc())
    )).all()

    today_requests = (await session.execute(
        select(func.coalesce(func.sum(TokenUsage.requests), 0)).where(
            TokenUsage.owner_telegram_id == owner_telegram_id,
            func.date(TokenUsage.created_at) == func.current_date(),
        )
    )).scalar_one()

    recent = (await session.execute(
        select(TokenUsage.kind, TokenUsage.input_tokens, TokenUsage.output_tokens, TokenUsage.created_at)
        .where(TokenUsage.owner_telegram_id == owner_telegram_id)
        .order_by(TokenUsage.created_at.desc())
        .limit(5)
    )).all()

    used = int(totals[0])
    return {
        "used": used,
        "limit": FREE_TIER_TOKEN_LIMIT,
        "remaining": max(FREE_TIER_TOKEN_LIMIT - used, 0),
        "percent_used": round(used / FREE_TIER_TOKEN_LIMIT * 100, 1) if FREE_TIER_TOKEN_LIMIT else 0.0,
        "total_requests": int(totals[1]),
        "operations": int(totals[2]),
        "today_requests": int(today_requests),
        "by_kind": [(k, int(t), int(n)) for k, t, n in by_kind],
        "recent": [(k, int(i) + int(o), ts) for k, i, o, ts in recent],
    }
