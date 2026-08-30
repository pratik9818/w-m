"""What each plan costs, and what it buys.

One catalogue, read by four places that must never disagree: the `/upgrade` buttons, the
checkout page a customer actually pays on, the quota checks that gate an edit, and the
`/plan` summary. Prices lived in three of those during the first draft and drifted within a
day, which is the entire reason this file exists.

The numbers come from measured cost, not from a competitor's price list. Reading a message
and making one change to a site costs about ₹17 all-in -- that figure already includes the
three-and-a-bit messages of conversation that surround an average edit, because a plan
priced against the edit alone is priced against the wrong number. Building a site from
scratch costs about ₹18.

Every allowance below is set so that a customer who uses every last change in a month still
leaves a margin. That is the design rule, and it is worth stating plainly: no single
account can go badly wrong. Starter at full tilt costs ₹680 of the ₹975 that survives
Razorpay's cut; Business at full tilt costs ₹1,450 of ₹1,952.

Sites are nearly free (₹18 once) and changes are not (₹17 every time), which is why
Business buys five sites but only twice the changes.
"""
from dataclasses import dataclass

# Weights. An owner is only ever shown the word "changes"; this is what one of theirs
# costs. Style edits are genuinely free because they are applied deterministically and
# never reach the model -- that is a real feature, not a rounding-down.
WEIGHT_STYLE = 0
WEIGHT_QUESTION = 0
WEIGHT_CONTENT = 1
WEIGHT_STRUCTURE = 2
WEIGHT_REBUILD = 5

# Asking the bot questions costs ~₹2 a time and consumes no allowance, so it needs its own
# ceiling or it becomes the cheapest way to run up a bill.
QUESTIONS_PER_DAY = 30
# Stops a runaway loop, and keeps republishes inside Cloudflare Pages' monthly deploy
# budget -- every change is a deployment, and that ceiling is account-wide.
CHANGES_PER_DAY = 10


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    # Paise, because that is the unit Razorpay speaks and rupee floats invite rounding bugs.
    monthly_paise: int
    yearly_paise: int
    sites: int
    changes: int
    # A second, hidden ceiling in tokens. The change counter treats every change as equal;
    # this is what catches the pathological one that costs ₹40 instead of ₹17. It is set
    # well above honest maximum use -- it should fire for abuse and for nothing else.
    token_ceiling: int
    # False for the free plan, whose allowance is a lifetime total rather than a monthly one.
    recurring: bool
    blurb: str
    perks: tuple[str, ...]

    @property
    def monthly_rupees(self) -> int:
        return self.monthly_paise // 100

    @property
    def yearly_rupees(self) -> int:
        return self.yearly_paise // 100


# TEMPORARY -- development settings, restore before anyone else can sign up.
#
# The free plan is priced as an acquisition cost with a hard ceiling: one site and five
# changes, about ₹103 at worst. These three values lift that ceiling so the owner's own
# account can be used for real testing, and they are placeholders rather than pricing
# decisions. At ~₹18 a build and ~₹17 a change, a single free signup can now cost about
# ₹2,150 before paying anything, against ₹103 at the real values.
#
# The token ceiling has to move with the change count or it becomes the real limit: it is
# the second, hidden guard meant to catch the pathological account, and at 400k it would
# stop this one after five or six changes with a message that deliberately explains
# nothing. 80k per change is the same ratio the free plan already used.
#
# Restore all three together -- 1, 5 and 400_000 -- and the bounded-acquisition-cost
# property in tests/test_billing.py tightens back up with them.
FREE_SITES_WHILE_TESTING = 25
FREE_CHANGES_WHILE_TESTING = 100
FREE_TOKEN_CEILING_WHILE_TESTING = 8_000_000

FREE = Plan(
    code="free",
    name="Free",
    monthly_paise=0,
    yearly_paise=0,
    sites=FREE_SITES_WHILE_TESTING,
    changes=FREE_CHANGES_WHILE_TESTING,
    token_ceiling=FREE_TOKEN_CEILING_WHILE_TESTING,
    recurring=False,
    blurb="One website, five changes, no card needed.",
    perks=(
        "1 website, live on the internet",
        "5 changes in total — not per month",
        "Colour and font tweaks always free",
        "A small “made with” line in the footer",
    ),
)

STARTER = Plan(
    code="starter",
    name="Starter",
    monthly_paise=99_900,
    yearly_paise=999_000,
    sites=1,
    changes=40,
    token_ceiling=2_500_000,
    recurring=True,
    blurb="For one business that wants its site to stay current.",
    perks=(
        "1 website",
        "40 changes every month",
        "Colour and font tweaks always free",
        "Connect your own domain name",
        "Enquiry form, with alerts here in chat",
        "Visitor numbers whenever you ask",
        "Footer line removed",
        "One full redesign a month",
    ),
)

BUSINESS = Plan(
    code="business",
    name="Business",
    monthly_paise=199_900,
    yearly_paise=1_999_000,
    sites=5,
    changes=80,
    token_ceiling=5_000_000,
    recurring=True,
    blurb="For several shops, or an agency running sites for clients.",
    perks=(
        "5 websites",
        "80 changes a month, shared across all of them",
        "Everything in Starter",
        "Your builds jump the queue",
        "Enquiries exported whenever you ask",
        "A visitor report sent to you monthly",
        "Three full redesigns a month",
    ),
)

PLANS: dict[str, Plan] = {p.code: p for p in (FREE, STARTER, BUSINESS)}
PAID_PLANS: tuple[Plan, ...] = (STARTER, BUSINESS)

PERIODS = ("monthly", "yearly")

# Top-ups are deliberately dearer per change than the plan they sit on: ₹19.90 against a
# ₹17 cost. They exist to rescue a month that ran short, not to be a cheaper way to buy
# what the next tier sells.
TOPUP_PAISE = 19_900
TOPUP_CHANGES = 10


def get_plan(code: str) -> Plan:
    return PLANS.get(code, FREE)


def price_paise(plan: Plan, period: str) -> int:
    return plan.yearly_paise if period == "yearly" else plan.monthly_paise


def price_rupees(plan: Plan, period: str) -> int:
    return price_paise(plan, period) // 100


# Operations that cost the owner nothing. `set_style` is here because it is applied
# deterministically and never reaches the model -- the promise that colour and font tweaks
# are always free is not marketing, it is what the code already does. The other two end in
# a question rather than a change.
FREE_OPERATIONS = frozenset({"set_style", "set_theme", "not_an_edit", "clarify"})

# A whole new site, at roughly ₹18 plus the risk of a repair afterwards.
REBUILD_OPERATIONS = frozenset({"rebuild_site", "change_layout"})

# Bigger than a text edit: a new form, or a photograph found and placed, rewrites more
# than one file.
STRUCTURAL_OPERATIONS = frozenset({"add_form", "add_photo", "add_policies"})


def weight_for_operation(operation: str) -> int:
    """How much of the monthly allowance one applied edit consumes.

    Deliberately generous at the edges: anything unrecognised counts as one change rather
    than as the maximum, because a charge the owner cannot explain costs more in trust
    than it saves in tokens.
    """
    if operation in FREE_OPERATIONS:
        return WEIGHT_STYLE
    if operation in REBUILD_OPERATIONS:
        return WEIGHT_REBUILD
    if operation in STRUCTURAL_OPERATIONS:
        return WEIGHT_STRUCTURE
    return WEIGHT_CONTENT
