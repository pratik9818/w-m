"""One row per class of failure, so a pattern is visible without reading any logs.

This is the whole of phase 2, and there is no intelligence in it. It is a GROUP BY.

That is worth stating plainly, because the failure it prevents was expensive and entirely
mundane. A CSS scanner stopped reading each stylesheet at the first apostrophe in a
comment. It broke style edits on three live sites. Every single failure was caught by a
postcondition check, written to edit_log, and reported to the owner in plain English. The
information was complete and it was never once aggregated, so nobody could see that six
separate "sorry, I couldn't do that" messages were one bug. An owner asked three times in
a row before it surfaced -- by which point they had concluded the bot was broken, which it
was.

A single line reading

    set_style: applied but not present in the stylesheet   6x   3 sites   since 21 Aug

would have ended that on day one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Business, EditLog, EditOutcome
from worker.learning.resolved import note_for, resolved_on
from worker.learning.signatures import (
    FAULT_CODE,
    FAULT_MODEL,
    FAULT_UNKNOWN,
    is_a_real_failure,
)

DEFAULT_DAYS = 14
# Enough to recognise the fault, few enough to stay readable in a terminal.
EXAMPLES_PER_SIGNATURE = 3


@dataclass
class LedgerRow:
    signature: str
    fault: str
    count: int
    sites: set[str] = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    examples: list[str] = field(default_factory=list)
    # Occurrences dated after the fix for this signature landed. Not a known issue --
    # a regression, which is a louder thing and is reported separately.
    since_fix: int = 0

    @property
    def site_count(self) -> int:
        return len(self.sites)

    @property
    def is_resolved(self) -> bool:
        return resolved_on(self.signature) is not None

    @property
    def is_regression(self) -> bool:
        return self.is_resolved and self.since_fix > 0


async def failure_rows(session: AsyncSession, days: int = DEFAULT_DAYS) -> list[LedgerRow]:
    """Every failure class seen in the window, worst first.

    Ordered by how many *sites* it touches before how many times it fired: a fault hitting
    three owners is a different thing from one owner hitting the same wall three times,
    and the first is nearly always the more urgent.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        select(EditOutcome, EditLog.raw_message, Business.name)
        .join(EditLog, EditLog.id == EditOutcome.edit_log_id)
        .outerjoin(Business, Business.id == EditOutcome.business_id)
        .where(EditOutcome.occurred_at >= cutoff, EditOutcome.signature.isnot(None))
        .order_by(EditOutcome.occurred_at)
    )

    rows: dict[str, LedgerRow] = {}
    for outcome, raw_message, business_name in result.all():
        if not is_a_real_failure(outcome.fault):
            continue
        row = rows.get(outcome.signature)
        if row is None:
            row = LedgerRow(signature=outcome.signature, fault=outcome.fault, count=0)
            rows[outcome.signature] = row
        row.count += 1
        row.sites.add(business_name or "(deleted site)")
        row.first_seen = row.first_seen or outcome.occurred_at
        row.last_seen = outcome.occurred_at
        fixed_at = resolved_on(outcome.signature)
        if fixed_at is not None and outcome.occurred_at >= fixed_at:
            row.since_fix += 1
        # "Yes" and "ok" are half the messages in a confirmation flow and say nothing
        # about what was being asked for. Prefer a message that names the request.
        example = " ".join((raw_message or "").split())
        if len(row.examples) < EXAMPLES_PER_SIGNATURE and len(example) > 12:
            row.examples.append(example[:90])

    return sorted(rows.values(), key=lambda r: (r.site_count, r.count), reverse=True)


async def health_counts(session: AsyncSession, days: int = DEFAULT_DAYS) -> dict[str, int]:
    """How the window's edits ended up, by label. The denominator for everything else."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        select(EditOutcome.label, func.count())
        .where(EditOutcome.occurred_at >= cutoff)
        .group_by(EditOutcome.label)
    )
    return {label: count for label, count in result.all()}


_FAULT_HEADINGS = {
    FAULT_CODE: "CODE DEFECTS -- the deterministic layer checked its own work and found it undone",
    FAULT_MODEL: "MODEL PROBLEMS -- the code did as it was told; the reading of the owner was wrong",
    FAULT_UNKNOWN: "UNCLASSIFIED -- no rule matches these yet; add one in signatures.py",
}


def render(rows: list[LedgerRow], counts: dict[str, int], days: int) -> str:
    """The ledger as a terminal report.

    Grouped by fault rather than sorted into one list, because the two groups go to
    different places: the first is a bug queue, the second is a prompt or parser problem.
    Mixing them is how a broken scanner gets treated as a comprehension failure.
    """
    total = sum(counts.values())
    lines = [
        f"Edit outcomes, last {days} days",
        "=" * 74,
    ]

    if not total:
        lines.append("\nNo edits in this window.")
        return "\n".join(lines)

    order = ["applied", "reasked", "failed", "clarify", "clarify_loop",
             "superseded", "proposed", "answered"]
    lines.append("")
    for label in order:
        if label not in counts:
            continue
        share = counts[label] / total * 100
        lines.append(f"  {label:<14} {counts[label]:>4}  ({share:>4.0f}%)")
    for label, count in sorted(counts.items()):
        if label not in order:
            lines.append(f"  {label:<14} {count:>4}")
    lines.append(f"  {'total':<14} {total:>4}")

    if not rows:
        lines += ["", "-" * 74, "", "No failures in this window."]
        return "\n".join(lines)

    regressions = [row for row in rows if row.is_regression]
    if regressions:
        # Above everything else, and worded as its own category. A failure dated after
        # its own fix is not a known issue that can wait for the queue -- it means the
        # fix did not hold, which is the single most important thing this can report.
        lines += ["", "=" * 74, "", "REGRESSIONS -- these were fixed and are happening again", ""]
        for row in regressions:
            lines.append(f"  {row.signature}")
            lines.append(f"      {row.since_fix}x since the fix on {resolved_on(row.signature):%d %b %H:%M}")
            lines.append(f"      fix was: {note_for(row.signature)}")
            lines.append("")

    for fault in (FAULT_CODE, FAULT_MODEL, FAULT_UNKNOWN):
        group = [row for row in rows if row.fault == fault]
        if not group:
            continue
        lines += ["", "-" * 74, "", _FAULT_HEADINGS[fault], ""]
        for row in group:
            span = ""
            if row.first_seen and row.last_seen:
                span = (f"  {row.first_seen:%d %b}"
                        + (f" - {row.last_seen:%d %b}"
                           if row.last_seen.date() != row.first_seen.date() else ""))
            sites = "1 site" if row.site_count == 1 else f"{row.site_count} sites"
            status = ""
            if row.is_regression:
                status = "   ** REGRESSED **"
            elif row.is_resolved:
                status = f"   [fixed {resolved_on(row.signature):%d %b}]"
            lines.append(f"  {row.signature}{status}")
            lines.append(f"      {row.count}x   {sites}{span}")
            lines.append(f"      sites: {', '.join(sorted(row.sites))}")
            for example in row.examples:
                lines.append(f"      > {example}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def needs_attention(rows: list[LedgerRow]) -> bool:
    """Is there anything here a person has to act on?

    A resolved defect showing historical occurrences does not count -- that is the ledger
    remembering, not the bot breaking. A regression always counts.
    """
    return any(
        row.is_regression or (row.fault == FAULT_CODE and not row.is_resolved)
        for row in rows
    )
