"""let a site take enquiries, and keep them somewhere the owner can ask for them.

Every generated site so far has ended at the same place: a phone number and an email
address. That works for the visitor who is ready to phone a stranger, and loses everyone
else -- the one browsing at eleven at night, the one who wants to send three sentences and
get on with their evening. The prompts said so outright ("no `<form>` elements: there is no
server to receive a submission"), which was true, and is what this changes.

Three columns and one table:

  - `businesses.forms` is what the site is supposed to have. A form is defined once, by the
    owner asking for it in chat, and lives here as a definition rather than as markup --
    so a rebuild that rewrites every page from scratch puts the same form back, with the
    same fields, instead of losing it.
  - `businesses.form_key` is how a page identifies itself when it posts. It is public (it
    ships inside the page's own script, as it must) and it is not a password: it names the
    site, it does not prove anything about who is posting. Its value is that it can be
    changed. A business id cannot -- it is in a dozen other places -- so a site being
    spammed had no remedy short of taking the form down.
  - `form_submissions` is what actually came in.

The payload is jsonb because the fields are the owner's to choose: "just name and a
message" and a nine-field intake form are both ordinary requests, and a table with a
`name` and an `email` column would have to be migrated every time somebody wanted a third.

`notified_at` records that the owner has been told, not that the row exists. The two come
apart exactly when it matters -- the submission is written first and the Telegram message
sent after, so a failure between them leaves an enquiry that is saved and unannounced,
which is recoverable, rather than announced and lost, which is not.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "forms",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("businesses", sa.Column("form_key", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_businesses_form_key", "businesses", ["form_key"])

    op.create_table(
        "form_submissions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("form_name", sa.String(length=40), nullable=False, server_default="contact"),
        # Which page it was sent from, for a site with more than one form on it.
        sa.Column("page", sa.String(length=40), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Every read of this table is "this business's enquiries, newest first" -- the chat
    # answer, the per-minute abuse ceiling, and the count in the owner's facts.
    op.create_index(
        "idx_form_submissions_business_time",
        "form_submissions",
        ["business_id", sa.text("submitted_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_form_submissions_business_time", table_name="form_submissions")
    op.drop_table("form_submissions")
    op.drop_constraint("uq_businesses_form_key", "businesses", type_="unique")
    op.drop_column("businesses", "form_key")
    op.drop_column("businesses", "forms")
