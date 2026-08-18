import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(String(60), nullable=False)
    # What the tokens were spent on: 'create' | 'edit' | 'rebuild' | 'parse'.
    # ('parse' is understanding the owner's chat message; the rest is writing the site.)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="create")
    # How many API calls this operation took. Tracked separately from tokens because the
    # provider's real ceiling is requests-per-day, not tokens -- a distinction that caused
    # genuine confusion when builds started failing with quota left over.
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
