import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class FormSubmission(Base):
    """One enquiry a visitor sent from a generated site.

    Written by the Supabase edge function that the page posts to, not by this codebase --
    which is why nothing here writes one. A static site on Cloudflare Pages has no server
    of its own, and the bot has no public address to receive a POST at, so the function is
    the only thing standing between the visitor and this table. It is read back by
    bot_api/services/form_data.py when the owner asks what has come in.

    `payload` holds whatever the form asked for, keyed by field name, because the fields
    are the owner's to choose -- see the migration for why that is not columns.
    """

    __tablename__ = "form_submissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    form_name: Mapped[str] = mapped_column(String(40), nullable=False, default="contact")
    page: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set once the owner has been told. Null means the enquiry arrived but the Telegram
    # message did not go out -- the recoverable half of that failure, on purpose.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
