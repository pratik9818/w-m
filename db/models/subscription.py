import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Subscription(Base):
    """What an owner is currently entitled to, and what Razorpay thinks about it.

    Keyed on the owner, not the business. An owner on Business has five sites and one
    subscription; putting the plan on `businesses` (where an unused `plan` column still
    sits, from an earlier guess) would have meant five rows that can disagree with each
    other about what one person has paid for.

    Exactly one row per owner. Upgrades rewrite it rather than appending, because the
    question this table answers is always "what can they do right now" -- the history of
    what they paid lives in `payments`, which is the table an accountant would want.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_telegram_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    # 'free' | 'starter' | 'business' -- a key into bot_api/services/plans.PLANS.
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    period: Mapped[str] = mapped_column(String(10), nullable=False, default="monthly")
    # 'active' | 'pending' | 'halted' | 'cancelled'. 'pending' is the gap between somebody
    # tapping pay and the mandate being authorised, which on UPI can be minutes.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(60), index=True)
    razorpay_customer_id: Mapped[str | None] = mapped_column(String(60))

    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when somebody cancels mid-month. They keep everything until the period ends --
    # they paid for it -- so this is a flag rather than an immediate downgrade.
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # When a failed mandate stops being survivable and the account actually drops to free.
    # Null unless the subscription is halted.
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Changes bought on top of the plan. Survives a period roll on purpose: somebody who
    # paid ₹199 in the last week of the month should not lose it at midnight.
    topup_changes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
