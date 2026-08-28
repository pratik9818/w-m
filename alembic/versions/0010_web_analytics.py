"""give each site a Cloudflare Web Analytics identity so its owner can be told who visited.

Owners ask "how many people came to my site today?" -- a question the bot could not answer
at all, because nothing was ever counting. Cloudflare counts it for free, but only for
sites that carry its beacon, and the beacon needs a token that is issued per hostname and
never changes afterwards. So it is issued once, at the first deploy, and stored here.

site_tag identifies the site when reading numbers back; site_token is what goes in the
page. Both nullable and never backfilled: sites deployed before this exist happily with
neither, and start counting from the next time they are deployed -- there is no way to
recover visits from before anything was watching, and pretending otherwise would be worse
than saying so.

enabled_at is what makes "no visits this month" honest. Without it, a site that only
started counting yesterday reports a truthful zero for last month that reads as "nobody
came", when the real answer is "nobody was counting".

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("cf_rum_site_tag", sa.String(length=64), nullable=True))
    op.add_column("businesses", sa.Column("cf_rum_site_token", sa.String(length=64), nullable=True))
    op.add_column(
        "businesses",
        sa.Column("analytics_enabled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("businesses", "analytics_enabled_at")
    op.drop_column("businesses", "cf_rum_site_token")
    op.drop_column("businesses", "cf_rum_site_tag")
