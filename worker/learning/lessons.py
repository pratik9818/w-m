"""What has already worked on *this* site, handed back to the parser as examples.

This is the first thing in the package that changes what the bot does, and it does so by
adding facts rather than by rewriting the bot. That distinction is the whole safety
argument: a lesson is a row anyone can read, it is per-site, and deleting it puts things
back exactly as they were.

The failure it targets is the commonest one there is. Every site has its own class names,
invented fresh by the model that wrote the stylesheet, so "the book a call button" is
`.btn-primary` on one site and `.cta-button` on the next. Today the parser works that out
from scratch on every single message and sometimes gets it wrong -- and when it does, the
owner is told their change was made, sees nothing happen, and asks again. Eleven of the
last ninety days' edits ended that way.

An owner's own history is the best possible answer to that question, and it is already
stored. If "make the book a call button smaller" resolved to `.btn-primary` last week and
the owner did not come back to complain, that is not a guess about this site -- it is the
answer, and it should not have to be rediscovered.

Two rules keep it honest:

  **Only edits that actually satisfied someone are taught.** An edit the owner had to ask
  for twice is labelled `reasked` and is excluded, because teaching a wrong answer is
  strictly worse than teaching nothing. Silence from the owner is the signal.

  **Cheap or not at all.** No model call, no embeddings, no extra round trip in the
  request path -- one indexed query and some string overlap. The whole injection is
  capped, because a two-year-old site must not cost more per message than a new one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EditLog, EditOutcome
from worker.learning.outcomes import LABEL_APPLIED

# How many examples reach the prompt. Six is enough to establish this site's vocabulary
# and small enough that the cost stays flat as a site's history grows -- which is the
# thing that would otherwise quietly erode the margin this feature is justified by.
MAX_LESSONS = 6

# Only pull recent history into memory to rank. A site with a thousand edits should not
# read a thousand rows to choose six.
CANDIDATE_LIMIT = 120

# Below this, an overlap is coincidence. "make" and "the" appear in every message ever
# sent, so a pair sharing only those has told us nothing.
MIN_RELEVANCE = 0.18

# Words too common to carry meaning about *which* part of a site is being discussed.
_STOPWORDS = frozenset("""
a an and are as at be but by can could do does for from get give go has have i in is it
its just make me my new no not of on or please put should so than that the their them then
there these they this to up us want was we what when where which who will with would you
your it's dont don't can't im i'm
""".split())

_WORD_RE = re.compile(r"[a-z][a-z0-9'-]*")


def _keywords(text: str) -> set[str]:
    return {
        word for word in _WORD_RE.findall((text or "").lower())
        if word not in _STOPWORDS and len(word) > 2
    }


def relevance(message: str, past: str) -> float:
    """How much these two messages are about the same thing, 0 to 1.

    Jaccard overlap on content words. Deliberately crude: it runs on every message, it
    must never be the reason a reply is slow, and the ranking only has to be good enough
    to put the right handful at the top of a list of six.
    """
    left, right = _keywords(message), _keywords(past)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass
class Lesson:
    """One thing that worked on this site, in the owner's words and the bot's."""

    message: str
    operation: str
    detail: str
    score: float

    def render(self) -> str:
        body = f'  "{self.message}" -> {self.operation}'
        return f"{body} ({self.detail})" if self.detail else body


def _describe(operation: dict) -> tuple[str, str]:
    """(what was done, the part worth remembering).

    For a style change the detail is the whole point: it records which class this owner's
    words turned out to mean, which is the fact the parser keeps having to rediscover.
    """
    name = str(operation.get("operation") or "")

    if name == "set_style":
        changes = operation.get("changes") or []
        parts = [
            f"{change.get('selector')} {change.get('property')}"
            for change in changes[:3]
            if isinstance(change, dict) and change.get("selector")
        ]
        return name, ", ".join(parts)

    if name == "patch_site":
        targets = operation.get("targets") or []
        return name, ", ".join(str(target) for target in targets[:3])

    if name in ("update_business_info", "add_service", "update_extra_instructions"):
        field = operation.get("field") or operation.get("name") or ""
        return name, str(field)

    return name, ""


def candidate_query(business_id):
    """Past edits on this site that are allowed to teach.

    A named function rather than an inline statement so a test can compile it and check
    the one condition that matters: only edits labelled `applied` are eligible. An edit
    the owner had to ask for twice carries the label `reasked` precisely so it can be
    excluded here, and teaching a wrong answer is strictly worse than teaching nothing.
    """
    return (
        select(EditLog.raw_message, EditLog.parsed_operation)
        .join(EditOutcome, EditOutcome.edit_log_id == EditLog.id)
        .where(
            EditOutcome.business_id == business_id,
            EditOutcome.label == LABEL_APPLIED,
            EditLog.parsed_operation.isnot(None),
        )
        .order_by(EditOutcome.occurred_at.desc())
        .limit(CANDIDATE_LIMIT)
    )


def rank(rows, message: str, limit: int = MAX_LESSONS) -> list[Lesson]:
    """Score, de-duplicate and cut down the candidates. Pure, so it is directly testable."""
    scored: list[Lesson] = []
    seen: set[tuple[str, str]] = set()
    for raw_message, operation in rows:
        if not isinstance(operation, dict):
            continue
        name, detail = _describe(operation)
        if not name:
            continue
        # One site asked for the same thing on four pages; four identical lines teach no
        # more than one and crowd out the rest.
        key = (name, detail)
        if key in seen:
            continue
        score = relevance(message, raw_message)
        if score < MIN_RELEVANCE:
            continue
        seen.add(key)
        scored.append(Lesson(
            message=" ".join((raw_message or "").split())[:120],
            operation=name,
            detail=detail,
            score=score,
        ))

    scored.sort(key=lambda lesson: lesson.score, reverse=True)
    return scored[:limit]


async def lessons_for(
    session: AsyncSession, business_id, message: str, limit: int = MAX_LESSONS
) -> list[Lesson]:
    """The most relevant past successes on this site, best first.

    Returns an empty list for a site with no history, which is the common case early on
    and costs one indexed query to discover.
    """
    rows = (await session.execute(candidate_query(business_id))).all()
    return rank(rows, message, limit)


def render_lessons(lessons: list[Lesson]) -> str:
    """The section handed to the parser, or "" when there is nothing to say.

    Worded as evidence rather than instruction. These are things that worked once, not
    rules -- an owner is perfectly entitled to mean something new by the same words, and
    a prompt that says "always" would stop the parser noticing.
    """
    if not lessons:
        return ""
    lines = [
        "",
        "Things that have worked on this site before, in this owner's own words. These are "
        "what their phrasing turned out to mean last time -- strong evidence about which "
        "part of the page they mean, not a rule. If this message is clearly about "
        "something else, ignore them.",
    ]
    lines += [lesson.render() for lesson in lessons]
    return "\n".join(lines) + "\n"


def estimate_tokens(rendered: str) -> int:
    """Rough size of the injection, for the cost guard in the tests."""
    return len(rendered) // 4
