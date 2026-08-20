"""add media.content_hash so re-sending the same picture is recognised.

An owner re-sent a photo they had already put on their site. Telegram hands out a fresh
file id every time an image is uploaded again (three real uploads of one picture produced
three different ids), so nothing spotted it: the bot asked where to put it as if it were
new, then added a second copy of the same photo to the page, which the owner then had to
ask to have removed.

Hashing the bytes is what actually identifies a picture. Nullable with no backfill --
rows predating this are simply never matched, which is correct rather than a gap.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("media", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_index("idx_media_business_hash", "media", ["business_id", "content_hash"])


def downgrade() -> None:
    op.drop_index("idx_media_business_hash", table_name="media")
    op.drop_column("media", "content_hash")
