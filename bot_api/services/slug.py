from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Business

RESERVED_SLUGS = {
    "api",
    "admin",
    "static",
    "webhook",
    "health",
    "telegram",
    "www",
    "app",
    "assets",
    "favicon.ico",
}


async def generate_unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)[:100] or "business"
    if base in RESERVED_SLUGS:
        base = f"{base}-site"

    candidate = base
    suffix = 1
    while await _slug_taken(session, candidate):
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


async def _slug_taken(session: AsyncSession, slug: str) -> bool:
    if slug in RESERVED_SLUGS:
        return True
    result = await session.execute(select(Business.id).where(Business.slug == slug))
    return result.scalar_one_or_none() is not None
