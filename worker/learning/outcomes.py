"""Work out what became of each edit, from what the owner did next.

Never ask the owner whether it worked. They have already told us, in the only currency
that cannot be gamed: what they typed next. Four behaviours carry almost all the signal,
and every one of them is already in the database.

  applied then asked again   The edit ran, and an hour later they asked for the same thing.
                             Technically a success; actually a failure, and the only label
                             here that catches an edit which did something -- just not the
                             thing that was wanted.
  a postcondition breach     The code checked its own work and found it undone. A bug.
  two questions in a row     The bot asked, was answered, and asked again. It did not
                             understand, and the owner is now doing the work.
  proposed and abandoned     Offered a change and never got a yes.

The awkward case is the confirmation gate, which logs the same message twice on purpose --
once when the change is proposed, once when it is applied. Read naively that is a repeat,
and 29 of 146 rows would be miscounted as owners asking twice. What separates them is not
the gap but the applied flags: False then True is one interaction; True then True is an
owner who did not get what they asked for.

Cheap by construction. No model call, no network, and it re-derives everything from
scratch each run, so improving the rules below is a matter of running it again.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EditLog, EditOutcome
from worker.learning.signatures import (
    FAULT_MODEL,
    FAULT_NONE,
    classify,
)

logger = logging.getLogger(__name__)

# A proposal waits on the owner to say yes. They are on a phone, mid-shift; ten minutes is
# ordinary. Past two hours it is a fresh request that happens to use the same words.
CONFIRM_WINDOW = timedelta(hours=2)

# How long a repeat still counts as "that didn't work" rather than "I want that again".
# A real one arrived 71 minutes later; a day is generous without being meaningless.
REASK_WINDOW = timedelta(hours=24)

# Two clarifying questions this close together are one failure to understand, not two
# separate conversations.
CLARIFY_LOOP_WINDOW = timedelta(minutes=30)

# Owners rephrase rather than repeat: "reduce the size of book a call demo" became
# "reduce the size of book a call demo in desktop screen". Requiring an exact match would
# miss most genuine repeats; going much below this starts matching unrelated requests that
# share a few common words.
SIMILAR_ENOUGH = 0.80

LABEL_APPLIED = "applied"
LABEL_REASKED = "reasked"
LABEL_SUPERSEDED = "superseded"
LABEL_PROPOSED = "proposed"
LABEL_CLARIFY = "clarify"
LABEL_CLARIFY_LOOP = "clarify_loop"
LABEL_ANSWERED = "answered"
LABEL_FAILED = "failed"


@dataclass
class Verdict:
    label: str
    fault: str
    signature: str | None
    detail: dict


def _normalise(message: str) -> str:
    return " ".join((message or "").lower().split())


# Below this, containment means nothing: "yes" sits inside half the messages ever sent.
MIN_CHARS_FOR_CONTAINMENT = 15


def _similar(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    shorter, longer = sorted((left, right), key=len)
    # The commonest way an owner repeats themselves is to say it again with a qualifier
    # on the end -- "reduce the size of book a call demo" became the same words plus "in
    # desktop screen". That real pair scores 0.795 by ratio alone and would be missed by
    # any threshold high enough to be safe, so restatement is recognised as itself.
    if len(shorter) >= MIN_CHARS_FOR_CONTAINMENT and shorter in longer:
        return 1.0

    return SequenceMatcher(None, left, right).ratio()


def _is_clarify(row: EditLog) -> bool:
    return bool(row.error) and row.error.lstrip().lower().startswith("asked:")


def _find_echo(row: EditLog, later: list[EditLog]) -> tuple[EditLog, float] | None:
    """The next message on this site that is asking for the same thing again."""
    text = _normalise(row.raw_message)
    for candidate in later:
        if candidate.created_at <= row.created_at:
            continue
        if candidate.created_at - row.created_at > REASK_WINDOW:
            break
        score = _similar(text, _normalise(candidate.raw_message))
        if score >= SIMILAR_ENOUGH:
            return candidate, score
    return None


def judge(row: EditLog, later: list[EditLog], previous: EditLog | None) -> Verdict:
    """What became of this one edit attempt.

    `later` is every subsequent row for the same business in time order; `previous` is the
    one immediately before it. Both are needed because the verdict on an edit lives in its
    neighbours, not in the row itself.
    """
    signature, fault = classify(row.error)

    if _is_clarify(row):
        # A good question is not a failure. Two in a row is: the bot asked, was answered,
        # and still did not understand -- at which point the owner is doing the work.
        if (
            previous is not None
            and _is_clarify(previous)
            and row.created_at - previous.created_at <= CLARIFY_LOOP_WINDOW
        ):
            return Verdict(
                LABEL_CLARIFY_LOOP, FAULT_MODEL,
                "clarify: asked twice without understanding",
                {"previous_edit_log_id": str(previous.id),
                 "gap_seconds": int((row.created_at - previous.created_at).total_seconds())},
            )
        return Verdict(LABEL_CLARIFY, fault, signature, {})

    if row.error and signature and fault != FAULT_NONE:
        return Verdict(LABEL_FAILED, fault, signature, {"error": row.error[:300]})

    if row.error:
        # Recognised as not-a-failure: the assistant answering a question, mostly.
        return Verdict(LABEL_ANSWERED, FAULT_NONE, signature, {})

    echo = _find_echo(row, later)

    if row.applied:
        if echo is not None:
            # The edit ran and the owner asked for the same thing again. Nothing in the
            # system detected a problem, which is exactly why this label matters: it is
            # the only evidence that a technically successful edit missed the point.
            candidate, score = echo
            return Verdict(
                LABEL_REASKED, FAULT_MODEL, "outcome: applied, then asked for again",
                {"repeat_edit_log_id": str(candidate.id),
                 "similarity": round(score, 3),
                 "gap_seconds": int((candidate.created_at - row.created_at).total_seconds())},
            )
        return Verdict(LABEL_APPLIED, FAULT_NONE, None, {})

    # Not applied, no error: a change offered and awaiting a yes.
    if echo is not None:
        candidate, score = echo
        if candidate.applied and candidate.created_at - row.created_at <= CONFIRM_WINDOW:
            # The confirmation gate logging the same interaction twice. Counting this as a
            # repeat would turn 29 of 146 rows into imaginary failures.
            return Verdict(
                LABEL_SUPERSEDED, FAULT_NONE, None,
                {"applied_as_edit_log_id": str(candidate.id), "similarity": round(score, 3)},
            )

    return Verdict(LABEL_PROPOSED, FAULT_NONE, None, {})


async def label_edits(
    session: AsyncSession, since: datetime | None = None, rebuild: bool = False
) -> dict[str, int]:
    """Derive an outcome for every edit and store it. Returns a count per label.

    Idempotent: existing rows for the edits in range are replaced, so this can run on a
    schedule and be re-run by hand after the rules change without producing duplicates.
    """
    query = select(EditLog).order_by(EditLog.business_id, EditLog.created_at)
    if since is not None:
        query = query.where(EditLog.created_at >= since)
    rows = list((await session.execute(query)).scalars().all())
    if not rows:
        return {}

    by_business: dict[uuid.UUID, list[EditLog]] = {}
    for row in rows:
        by_business.setdefault(row.business_id, []).append(row)

    verdicts: dict[uuid.UUID, Verdict] = {}
    for business_rows in by_business.values():
        for index, row in enumerate(business_rows):
            verdicts[row.id] = judge(
                row,
                later=business_rows[index + 1:],
                previous=business_rows[index - 1] if index else None,
            )

    # Replace rather than upsert: the whole point is that re-running with better rules
    # produces a fresh answer, not a merge of the old one and the new.
    await session.execute(
        delete(EditOutcome).where(EditOutcome.edit_log_id.in_(list(verdicts)))
    )

    tally: dict[str, int] = {}
    for row in rows:
        verdict = verdicts[row.id]
        session.add(EditOutcome(
            edit_log_id=row.id,
            business_id=row.business_id,
            owner_telegram_id=row.telegram_user_id,
            label=verdict.label,
            fault=verdict.fault,
            signature=verdict.signature,
            detail=verdict.detail,
            occurred_at=row.created_at,
        ))
        tally[verdict.label] = tally.get(verdict.label, 0) + 1

    await session.commit()
    logger.info(
        "learning.labelled",
        extra={"event": "learning.labelled", "rows": len(rows), "tally": tally},
    )
    return tally
