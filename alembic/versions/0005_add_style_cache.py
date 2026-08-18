"""add businesses.style_css / style_signature -- cached stylesheet so content-only edits
skip regenerating the design (the largest and slowest generated artifact).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("style_css", sa.Text(), nullable=True))
    op.add_column("businesses", sa.Column("style_signature", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("businesses", "style_signature")
    op.drop_column("businesses", "style_css")
