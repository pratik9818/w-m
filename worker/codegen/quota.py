import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TokenUsage

FREE_TIER_TOKEN_LIMIT = 300_000


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
) -> None:
    session.add(
        TokenUsage(
            owner_telegram_id=owner_telegram_id,
            business_id=business_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )
    await session.commit()
