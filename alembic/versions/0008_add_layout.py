"""add businesses.layout so a site can be a single landing page instead of four pages.

Until now the four-page structure was hardcoded in five places, so "keep only one page,
this is a landing page" was impossible -- and nothing said so, it just quietly cost
21,867 tokens and changed nothing.

Existing sites are all four-page, which is what the default records.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("layout", sa.String(length=20), nullable=False, server_default="multipage"),
    )


def downgrade() -> None:
    op.drop_column("businesses", "layout")
