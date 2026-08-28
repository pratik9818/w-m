"""let a site carry a video and a PDF, not only photographs.

The media table has only ever allowed 'logo' and 'photo', enforced by a CHECK constraint,
so a restaurant could not put its menu on its own website and a salon could not show a
video of the room. Both are ordinary things to ask for, and the bot had no answer -- worse,
it had no *reply*: a PDF sent to the chat matched no handler at all and was met with
silence.

Widening the constraint is the whole schema change. Everything else -- size limits per
kind, where the file lands on the page -- lives in code, because those are decisions we
expect to revise and a CHECK constraint is a poor place to keep a decision that moves.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("media_kind_check", "media", type_="check")
    op.create_check_constraint(
        "media_kind_check", "media", "kind IN ('logo','photo','video','document')"
    )


def downgrade() -> None:
    # Anything stored under the new kinds would violate the old constraint, so it goes
    # first. Downgrading is losing the feature; it should not also mean a failed migration
    # that leaves the table half-changed.
    op.execute("DELETE FROM media WHERE kind IN ('video','document')")
    op.drop_constraint("media_kind_check", "media", type_="check")
    op.create_check_constraint("media_kind_check", "media", "kind IN ('logo','photo')")
