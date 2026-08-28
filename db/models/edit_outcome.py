import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class EditOutcome(Base):
    """What actually became of one edit attempt, worked out after the fact.

    edit_log records what the bot did. This records whether it worked -- which is not the
    same question and cannot be answered at the time, because the evidence arrives later:
    an owner who undoes the change two minutes on, or asks for the same thing again an
    hour later, has told us the edit failed no matter how successfully it ran.

    Kept in its own table rather than as columns on edit_log so the labelling job can be
    re-run from scratch after its rules improve, without touching the record of what
    happened. One row per edit_log row, at most.
    """

    __tablename__ = "edit_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    edit_log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("edit_log.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    # Denormalised so the ledger can group by site without joining back through edit_log.
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE")
    )
    owner_telegram_id: Mapped[int | None] = mapped_column(BigInteger)

    # What became of it: applied | reasked | superseded | proposed | clarify |
    # clarify_loop | answered | failed
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    # Whose problem it is: none | code | model | timing | unknown. See
    # worker/learning/signatures.py -- this is the distinction that decides whether a
    # failure is a bug report or a prompt problem.
    fault: Mapped[str] = mapped_column(String(12), nullable=False)
    # The class of failure, so many incidents collapse into one ledger row.
    signature: Mapped[str | None] = mapped_column(Text, index=True)
    # Evidence for the label -- the gap to the repeat, the id of the message that
    # superseded this one. Kept so a human reading the ledger can check the reasoning.
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # When the underlying edit happened, copied so the ledger can window by it directly.
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
