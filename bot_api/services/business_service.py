import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot_api.services.slug import generate_unique_slug
from db.models import Business, Media, Service, SiteVersion


async def get_live_files(session: AsyncSession, business: Business) -> dict[str, str] | None:
    """The exact files currently deployed for this business, if we have them stored.

    Mirrors the worker's own `_live_files`; the bot needs them so the edit parser can see
    the real page before deciding what to change.
    """
    if business.current_version_id is None:
        return None
    result = await session.execute(
        select(SiteVersion.files).where(SiteVersion.id == business.current_version_id)
    )
    return result.scalar_one_or_none() or None


async def list_businesses_for_owner(session: AsyncSession, owner_telegram_id: int) -> list[Business]:
    result = await session.execute(
        select(Business)
        .where(Business.owner_telegram_id == owner_telegram_id)
        .order_by(Business.created_at)
    )
    return list(result.scalars().all())


async def get_business_by_id(
    session: AsyncSession, business_id: uuid.UUID, owner_telegram_id: int | None = None
) -> Business | None:
    query = (
        select(Business)
        .options(selectinload(Business.services), selectinload(Business.media))
        .where(Business.id == business_id)
    )
    if owner_telegram_id is not None:
        query = query.where(Business.owner_telegram_id == owner_telegram_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


class OnboardingSpec:
    """Plain container for everything collected during the FSM, before it becomes DB rows."""

    def __init__(self) -> None:
        self.business_id: uuid.UUID = uuid.uuid4()
        self.name: str = ""
        self.category: str = ""
        self.tagline: str | None = None
        self.about: str | None = None
        self.services: list[dict] = []  # [{"name": ..., "price_label": ...}]
        self.phone: str | None = None
        self.email: str | None = None
        self.address: str | None = None
        self.hours_display_text: str | None = None
        self.logo: dict | None = None  # {"storage_path": ..., "url": ...}
        self.photos: list[dict] = []
        self.theme: str = "classic"
        self.layout: str = "multipage"


async def create_business_from_spec(
    session: AsyncSession, owner_telegram_id: int, spec: OnboardingSpec
) -> Business:
    slug = await generate_unique_slug(session, spec.name)

    business = Business(
        id=spec.business_id,
        owner_telegram_id=owner_telegram_id,
        slug=slug,
        name=spec.name,
        category=spec.category,
        tagline=spec.tagline,
        about=spec.about,
        theme=spec.theme,
        layout=spec.layout,
        phone=spec.phone,
        email=spec.email,
        address=spec.address,
        hours={"display_text": spec.hours_display_text} if spec.hours_display_text else {},
        social_links={},
        status="active",
        generation_status="queued",
    )
    session.add(business)

    for index, service in enumerate(spec.services):
        session.add(
            Service(
                business_id=spec.business_id,
                name=service["name"],
                price_label=service.get("price_label"),
                sort_order=index,
            )
        )

    media_items = []
    if spec.logo:
        media_items.append(("logo", spec.logo))
    for photo in spec.photos:
        media_items.append(("photo", photo))
    for index, (kind, media) in enumerate(media_items):
        session.add(
            Media(
                business_id=spec.business_id,
                kind=kind,
                storage_path=media["storage_path"],
                url=media["url"],
                sort_order=index,
            )
        )

    await session.commit()
    await session.refresh(business)
    return business
