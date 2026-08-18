"""add token_usage.kind and token_usage.requests so usage can be reported per operation.

`kind` separates writing a site from merely understanding a chat message; `requests`
tracks API calls, which is what the provider actually caps per day (tokens are our own
limit, requests are theirs -- conflating the two made a rate-limit failure look like a bug).

Existing rows are backfilled as 'create' with 1 request, which is what they were.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "token_usage",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="create"),
    )
    op.add_column(
        "token_usage",
        sa.Column("requests", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("token_usage", "requests")
    op.drop_column("token_usage", "kind")
