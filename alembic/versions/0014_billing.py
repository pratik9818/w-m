"""Plans, allowances and the record of what was paid.

Three tables, and the split between them is the design:

  - `subscriptions` is what somebody can do right now. One row per owner, rewritten on
    every upgrade. It is the table the quota check reads, so it is deliberately small.
  - `usage_periods` is the counting, kept after the month ends. A single mutable counter
    on the subscription would have been smaller and would have destroyed the only evidence
    that says whether 40 changes a month is the right number. After two months of real
    customers that becomes a query rather than an argument.
  - `payments` is what Razorpay told us, once. `razorpay_event_id` is unique and that
    constraint is the idempotency mechanism: webhooks are delivered more than once, and
    without it the second delivery of `subscription.charged` grants a second month.

Entitlement hangs off the owner rather than the business. `businesses.plan` has existed
and gone unread since the first migration, from an earlier guess that a plan belonged to a
site; it stays where it is rather than being dropped, but nothing reads it. An owner on
Business has five sites and one subscription, and five rows that can disagree about what
one person paid for is not a state worth being able to represent.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("owner_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=False, server_default="free"),
        sa.Column("period", sa.String(length=10), nullable=False, server_default="monthly"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("razorpay_subscription_id", sa.String(length=60), nullable=True),
        sa.Column("razorpay_customer_id", sa.String(length=60), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("topup_changes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_subscriptions_owner", "subscriptions", ["owner_telegram_id"]
    )
    # The webhook arrives knowing only Razorpay's id, and has to find the owner from it.
    op.create_index(
        "idx_subscriptions_razorpay_id", "subscriptions", ["razorpay_subscription_id"]
    )

    op.create_table(
        "usage_periods",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("owner_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=False, server_default="free"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        # Null on the free plan, whose allowance is a lifetime total and never rolls.
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changes_included", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changes_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Doubles as the lookup: every read is "this owner's period starting at T", and the
    # constraint is what makes get-or-create safe when two edits land at the same instant.
    op.create_unique_constraint(
        "uq_usage_period_owner_start", "usage_periods", ["owner_telegram_id", "period_start"]
    )
    op.create_index("idx_usage_periods_owner", "usage_periods", ["owner_telegram_id"])

    op.create_table(
        "payments",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Unique, and that is the whole point of this table -- see the module docstring.
        sa.Column("razorpay_event_id", sa.String(length=80), nullable=False),
        sa.Column("event", sa.String(length=60), nullable=False),
        sa.Column("owner_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("razorpay_subscription_id", sa.String(length=60), nullable=True),
        sa.Column("razorpay_payment_id", sa.String(length=60), nullable=True),
        sa.Column("amount_paise", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_payments_event_id", "payments", ["razorpay_event_id"])
    op.create_index("idx_payments_owner", "payments", ["owner_telegram_id"])
    op.create_index(
        "idx_payments_subscription", "payments", ["razorpay_subscription_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_payments_subscription", table_name="payments")
    op.drop_index("idx_payments_owner", table_name="payments")
    op.drop_constraint("uq_payments_event_id", "payments", type_="unique")
    op.drop_table("payments")

    op.drop_index("idx_usage_periods_owner", table_name="usage_periods")
    op.drop_constraint("uq_usage_period_owner_start", "usage_periods", type_="unique")
    op.drop_table("usage_periods")

    op.drop_index("idx_subscriptions_razorpay_id", table_name="subscriptions")
    op.drop_constraint("uq_subscriptions_owner", "subscriptions", type_="unique")
    op.drop_table("subscriptions")
