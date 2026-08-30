import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Payment(Base):
    """Every Razorpay event that moved money, stored once.

    `razorpay_event_id` is unique and that is the whole point of the table. Razorpay
    delivers webhooks more than once -- on its own retries, and again if our handler is
    slow enough to time out -- and without a uniqueness constraint the second delivery of
    `subscription.charged` grants a second month. The insert is the idempotency check:
    if it raises, the event has already been handled and there is nothing to do.

    `payload` keeps the raw event because reconciling a disputed charge six months from now
    against a schema we have since changed is not a position worth being in.
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    razorpay_event_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    event: Mapped[str] = mapped_column(String(60), nullable=False)

    owner_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(60), index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(60))

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
