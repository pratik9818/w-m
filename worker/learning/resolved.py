"""Failure classes that have been fixed, and the date the fix landed.

A ledger with no memory of what has been dealt with becomes noise within a fortnight: it
reopens every closed case every time it runs, and the one new fault gets lost among them.

This is a file rather than a table on purpose. A signature is marked resolved in the same
commit as the fix that resolved it, reviewed alongside it, and reverted with it. It also
gives the ledger something it could not otherwise say: a failure dated *after* its fix is
not a known issue, it is a regression, and that deserves to be shouted about rather than
quietly folded into a count.

Add an entry when you fix something. Never edit the time to silence a row -- if it is
firing again, the fix did not hold, and that is the most valuable thing this can tell you.

The time is the moment the fix went *live*, not the moment it was written, and it is a
timestamp rather than a date for a reason: a fault fixed at 11:11 was still failing at
11:02, and a day's granularity reports that morning as a regression. A tool that cries
regression on its first run is a tool nobody reads twice.
"""
from __future__ import annotations

from datetime import datetime, timezone

# signature -> (moment the fix went live in UTC, what was done)
RESOLVED: dict[str, tuple[datetime, str]] = {
    "set_style: applied but not present in the stylesheet": (
        datetime(2026, 8, 28, 11, 11, tzinfo=timezone.utc),
        "css_values.iter_rule_spans stopped reading at the first apostrophe in a comment, "
        "so rules below it were invisible and every edit to them failed verification",
    ),
    "patch_site: target list came out empty": (
        datetime(2026, 8, 28, 18, 30, tzinfo=timezone.utc),
        "the model returned targets as the string 'index.html, style.css' rather than an "
        "array; normalize_patch_targets iterated it character by character and matched "
        "nothing. edit_ops.coerce_targets now reads a comma-separated string as the list "
        "it plainly is",
    ),
}


def resolved_on(signature: str) -> datetime | None:
    entry = RESOLVED.get(signature)
    return entry[0] if entry else None


def note_for(signature: str) -> str | None:
    entry = RESOLVED.get(signature)
    return entry[1] if entry else None
