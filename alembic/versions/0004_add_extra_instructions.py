"""add businesses.extra_instructions -- free-text owner instructions for structurally
new site content ("add a testimonials section"), folded into every regeneration.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("extra_instructions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("businesses", "extra_instructions")
