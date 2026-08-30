"""let a site carry the policy pages a payment gateway insists on.

An owner who wants to take money online hits a wall none of them expect: Razorpay,
Cashfree and PayU all run automated checks on the merchant's website before activating the
account, and all of them look for the same four documents -- terms, privacy, refunds and
shipping -- plus a visible email, phone number and address. Missing one is an outright
rejection, usually with no indication of which one.

One column, for the same reason `forms` is a column rather than markup: the pages are
defined here and rendered onto the site by every build, so a redesign that rewrites all
four pages from scratch puts them back instead of quietly deleting the thing the owner's
payment approval depends on.

What it holds is settings, not text:

    {"enabled": true, "refund_days": 7, "legal_name": "...", "updated_on": "2026-08-30"}

The prose is generated in worker/codegen/policies.py from a template plus the business's
own contact details. Deliberately not stored: a policy page is a commitment, and text
frozen into a row at the moment it was written would keep saying "we refund within 7 days"
long after the owner changed their mind, on a page a payment aggregator has verified.

`legal_name` exists because the name on a Razorpay account is a registered entity and the
name on the website is usually a shopfront -- "Sharma Traders Pvt Ltd" against "Sharma
Sweets" -- and the verifier compares the two.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "policies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("businesses", "policies")
