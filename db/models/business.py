import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    tagline: Mapped[str | None] = mapped_column(String(120))
    about: Mapped[str | None] = mapped_column(Text)
    theme: Mapped[str] = mapped_column(String(20), nullable=False, default="classic")
    # 'multipage' (four linked pages) or 'landing' (one page, nav scrolls to sections).
    layout: Mapped[str] = mapped_column(String(20), nullable=False, default="multipage")
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(Text)
    hours: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    social_links: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    generation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("site_versions.id", use_alter=True)
    )
    deployment_url: Mapped[str | None] = mapped_column(String(255))
    cf_pages_project_name: Mapped[str | None] = mapped_column(String(120))
    # Cloudflare Web Analytics. Issued once per hostname on the first deploy and never
    # changed after: site_tag reads the numbers back, site_token goes in the page. Null on
    # sites deployed before this existed, and on any site whose provisioning failed --
    # analytics is never allowed to be the reason a deploy does not happen.
    cf_rum_site_tag: Mapped[str | None] = mapped_column(String(64))
    cf_rum_site_token: Mapped[str | None] = mapped_column(String(64))
    # When counting actually started, so "no visits last month" can be told apart from
    # "nothing was watching last month".
    analytics_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The forms this site is meant to carry, keyed by name: {"contact": {"title": ...,
    # "fields": [...], "page": "contact.html"}}. A definition rather than markup, so a
    # rebuild that rewrites every page from scratch puts the same form back instead of
    # losing it. Empty for every site whose owner has not asked for one.
    forms: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # How a page identifies itself when it posts an enquiry. Public by necessity -- it
    # ships inside the page's own script -- and therefore not a secret: it names the site,
    # it does not prove anything. It exists so a site being spammed can be given a new one
    # without touching its id, which is in a dozen other places.
    form_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Whether this site carries the four policy pages a payment gateway checks for, and
    # the couple of settings that change what they say:
    # {"enabled": true, "refund_days": 7, "legal_name": ..., "updated_on": "2026-08-30"}.
    # Settings rather than text -- the prose is rendered by every build from the business's
    # current contact details, so a page cannot go on promising something the owner has
    # since changed. Empty for every site whose owner has not asked for them.
    policies: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Durable design preferences only ("always use a green navbar"), applied on a full
    # build or an explicit rebuild. NOT a per-edit channel -- element-level changes are
    # one-shot patches against the stored files instead, so they can't be replayed and
    # reinterpreted on every future build.
    extra_instructions: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    services: Mapped[list["Service"]] = relationship(
        back_populates="business", cascade="all, delete-orphan", order_by="Service.sort_order"
    )
    media: Mapped[list["Media"]] = relationship(
        back_populates="business", cascade="all, delete-orphan", order_by="Media.sort_order"
    )
