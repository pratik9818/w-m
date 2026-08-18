import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class SiteVersion(Base):
    __tablename__ = "site_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)  # 'create' | 'edit'
    code_storage_path: Mapped[str | None] = mapped_column(String(500))
    # Snapshot of every generated file for this version, filename -> content. This is what
    # makes editing incremental: the next edit patches these bytes instead of regenerating
    # the site from the spec, so untouched pages stay byte-identical.
    files: Mapped[dict | None] = mapped_column(JSONB)
    sandbox_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    sandbox_report: Mapped[dict | None] = mapped_column(JSONB)
    deployed_url: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
