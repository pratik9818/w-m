"""add site_versions.files (per-version snapshot of every generated file) and drop the
businesses stylesheet cache it replaces.

The cache added in 0005 was superseded within the same session: its signature was
hash(theme + extra_instructions), and virtually every real element-level edit routes
through extra_instructions, so the cache was invalidated on essentially every edit and
the whole design got rebuilt anyway. site_versions.files now holds style.css along with
the pages, and edits patch those bytes -- keeping two sources of truth for the stylesheet
is how drift starts, so the old columns go.

No backfill: a business with no stored files simply takes one more full build, then
patches from then on.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("site_versions", sa.Column("files", postgresql.JSONB(), nullable=True))
    op.drop_column("businesses", "style_signature")
    op.drop_column("businesses", "style_css")


def downgrade() -> None:
    op.add_column("businesses", sa.Column("style_css", sa.Text(), nullable=True))
    op.add_column("businesses", sa.Column("style_signature", sa.String(length=64), nullable=True))
    op.drop_column("site_versions", "files")
