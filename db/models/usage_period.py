import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class UsagePeriod(Base):
    """One billing month's worth of counting, kept after it ends.

    A single mutable "changes used" number on the subscription would have been smaller and
    would have thrown away the only evidence that says whether the allowances are set
    correctly. Keeping a row per period means that after two months of real customers the
    question "is 40 changes too many or too few" has an answer in the database instead of
    an opinion.

    `period_end` is null for the free plan, whose five changes are a lifetime total rather
    than a monthly one -- so the row is opened once and never rolls.
    """

    __tablename__ = "usage_periods"
    __table_args__ = (
        UniqueConstraint("owner_telegram_id", "period_start", name="uq_usage_period_owner_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="free")

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    changes_included: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    changes_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Counted alongside the changes rather than derived from token_usage at read time:
    # the circuit breaker is checked before every single edit, and a sum over a growing
    # table is the wrong shape for that.
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
