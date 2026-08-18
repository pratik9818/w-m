"""rename businesses.vercel_project_id to cf_pages_project_name (Vercel -> Cloudflare Pages)

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("businesses", "vercel_project_id", new_column_name="cf_pages_project_name")


def downgrade() -> None:
    op.alter_column("businesses", "cf_pages_project_name", new_column_name="vercel_project_id")
