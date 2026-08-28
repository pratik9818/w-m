"""record what became of each edit, so failures can be counted instead of only logged.

The bot already detects its own failures and writes them down. Nothing reads them back,
which is how a broken CSS scanner disabled style edits on 3 of 15 live sites for days:
every failure was caught, logged, reported to the owner -- and then forgotten, one at a
time, so the pattern never appeared.

This table is the missing half. One row per edit attempt, carrying what became of it, who
is at fault, and the signature of the failure class it belongs to. Derived entirely from
data already stored, so it can be rebuilt from scratch whenever the labelling rules
improve -- which is why it is a separate table rather than columns on edit_log.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "edit_outcomes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "edit_log_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("edit_log.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "business_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
        ),
        sa.Column("owner_telegram_id", sa.BigInteger()),
        sa.Column("label", sa.String(length=20), nullable=False),
        sa.Column("fault", sa.String(length=12), nullable=False),
        sa.Column("signature", sa.Text()),
        sa.Column(
            "detail",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_outcomes_signature", "edit_outcomes", ["signature"])
    # The ledger's main query is "failures in the last N days, newest first".
    op.create_index("idx_outcomes_occurred", "edit_outcomes", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("idx_outcomes_occurred", table_name="edit_outcomes")
    op.drop_index("idx_outcomes_signature", table_name="edit_outcomes")
    op.drop_table("edit_outcomes")
