"""initial schema: businesses, services, media, site_versions, edit_log

Revision ID: 0001
Revises:
Create Date: 2026-08-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(10), nullable=False),
        sa.Column("code_storage_path", sa.String(500)),
        sa.Column("sandbox_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sandbox_report", postgresql.JSONB()),
        sa.Column("deployed_url", sa.String(255)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "businesses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("tagline", sa.String(120)),
        sa.Column("about", sa.Text()),
        sa.Column("theme", sa.String(20), nullable=False, server_default="classic"),
        sa.Column("phone", sa.String(40)),
        sa.Column("email", sa.String(120)),
        sa.Column("address", sa.Text()),
        sa.Column("hours", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("social_links", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("generation_status", sa.String(20), nullable=False, server_default="none"),
        sa.Column(
            "current_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("site_versions.id", use_alter=True),
        ),
        sa.Column("deployment_url", sa.String(255)),
        sa.Column("vercel_project_id", sa.String(120)),
        sa.Column("plan", sa.String(20), nullable=False, server_default="free"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_businesses_owner", "businesses", ["owner_telegram_id"])

    op.create_foreign_key(
        "fk_site_versions_business",
        "site_versions",
        "businesses",
        ["business_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_versions_business", "site_versions", ["business_id"])

    op.create_table(
        "services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("price_label", sa.String(60)),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_services_business", "services", ["business_id"])

    op.create_table(
        "media",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('logo','photo')", name="media_kind_check"),
    )
    op.create_index("idx_media_business", "media", ["business_id"])

    op.create_table(
        "edit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_message", sa.Text(), nullable=False),
        sa.Column("parsed_operation", postgresql.JSONB()),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "triggered_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("site_versions.id")
        ),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_edit_log_business", "edit_log", ["business_id"])


def downgrade() -> None:
    op.drop_table("edit_log")
    op.drop_table("media")
    op.drop_table("services")
    op.drop_constraint("fk_site_versions_business", "site_versions", type_="foreignkey")
    op.drop_table("businesses")
    op.drop_table("site_versions")
