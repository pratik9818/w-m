"""Turn a failure that really happened into a case that can never happen quietly again.

The corpus in cases.py is ten messages somebody typed out by hand. That was the right way
to start and it is the wrong way to continue: the bot produces its own material every day,
and the failures it has already had are better test cases than any that could be invented,
because they are the ones real owners actually sent.

What makes this practical here is that every version stores its complete file set. A
failure from last Tuesday can be replayed against the exact stylesheet the parser was
looking at when it failed -- offline, deterministically, for nothing.

Two boundaries this deliberately respects:

  **Only model faults become parser cases.** A code defect -- a postcondition breach like
  the CSS scanner that stopped reading at an apostrophe -- was not a parsing mistake. The
  parser was right and the executor was broken. Promoting those here would build a corpus
  that passes while the bug is still there, which is worse than no corpus. They are listed
  for a hand-written unit test instead.

  **A generated assertion is never invented.** We know what went wrong, not what should
  have happened. So a promoted case asserts only what the evidence supports: an edit the
  owner had to ask for twice must not produce the same operation again, and a question
  asked twice must not become a third. Anything stronger is a guess, and a guess baked
  into a permanent test is a future afternoon wasted.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import Business, EditLog, EditOutcome, SiteVersion
from worker.learning.outcomes import (
    LABEL_CLARIFY_LOOP,
    LABEL_FAILED,
    LABEL_REASKED,
)
from worker.learning.signatures import FAULT_MODEL

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REGRESSIONS_FILE = Path(__file__).parent / "regressions.json"

# Mirrors evals/export_fixture.py -- the shape run_evals.py expects.
BUSINESS_FIELDS = (
    "name", "category", "tagline", "about", "theme", "layout", "phone", "email",
    "address", "hours", "extra_instructions", "slug",
)

PROMOTABLE = {LABEL_REASKED, LABEL_CLARIFY_LOOP, LABEL_FAILED}


# How many preceding turns to carry. The same figure the live bot keeps, so a replay
# sees neither more nor less than production did.
CONTEXT_TURNS = 4


@dataclass
class Candidate:
    outcome: EditOutcome
    edit: EditLog
    business: Business
    version: SiteVersion
    context: list[dict]


def rebuild_context(before: list[EditLog]) -> list[dict]:
    """The conversation as session.py would have held it, rebuilt from edit_log.

    Without this the corpus is full of unanswerable cases. Four of the first failures
    promoted were the messages "Yed", "Yes you do it", "Single landing page" and "Get it
    from pixel" -- every one an answer to a question the bot had just asked. Replayed on
    their own they are not hard cases, they are meaningless ones, and a parser that failed
    them would be right to.
    """
    turns: list[dict] = []
    for row in before[-CONTEXT_TURNS:]:
        error = (row.error or "").strip()
        if error.lower().startswith("asked:"):
            outcome = {"bot_asked": error.split(":", 1)[1].strip()}
        elif row.applied:
            operation = (row.parsed_operation or {}).get("operation", "a change")
            outcome = {"applied": operation, "summary": "applied to the site"}
        elif error:
            outcome = {"rejected": error[:200]}
        else:
            # Proposed and awaiting a yes -- which is exactly the state that makes the
            # next message a bare "yes".
            operation = (row.parsed_operation or {}).get("operation")
            outcome = {"bot_asked": f"shall I go ahead with {operation}?"} if operation else {}
        turns.append({"raw_message": row.raw_message, "outcome": outcome})
    return turns


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "site"


def fixture_name_for(business: Business, version: SiteVersion) -> str:
    """One fixture per (site, version), so ten failures on one site share one snapshot."""
    return f"regression-{_slugify(business.slug or business.name)}-v{version.version_number}"


def case_id_for(outcome: EditOutcome) -> str:
    """Stable across runs, so re-promoting updates a case instead of duplicating it."""
    return f"{outcome.label}-{str(outcome.edit_log_id)[:8]}"


async def find_candidates(
    session: AsyncSession, days: int = 30, limit: int = 50
) -> list[Candidate]:
    """Failures worth turning into cases, newest first.

    The version chosen is the one that was live when the edit was attempted -- not the
    newest. Replaying a failure against a site that has since been rebuilt tests nothing
    the failure was about.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await session.execute(
        select(EditOutcome, EditLog, Business)
        .join(EditLog, EditLog.id == EditOutcome.edit_log_id)
        .join(Business, Business.id == EditOutcome.business_id)
        # Eagerly, because a fixture needs the services and media and async SQLAlchemy
        # will not lazy-load them behind our back -- it raises instead, which is the
        # better behaviour and the reason this is spelled out.
        .options(selectinload(Business.services), selectinload(Business.media))
        .where(
            EditOutcome.occurred_at >= cutoff,
            EditOutcome.label.in_(sorted(PROMOTABLE)),
        )
        .order_by(EditOutcome.occurred_at.desc())
    )).all()

    # Every message on a site, so the turns before a failure can be reconstructed.
    history: dict[uuid.UUID, list[EditLog]] = {}
    for business_id in {outcome.business_id for outcome, _, _ in rows}:
        history[business_id] = list((await session.execute(
            select(EditLog)
            .where(EditLog.business_id == business_id)
            .order_by(EditLog.created_at)
        )).scalars().all())

    candidates: list[Candidate] = []
    for outcome, edit, business in rows:
        # A code defect is not a parsing mistake, and a corpus that pretends otherwise
        # passes while the real bug is still in the tree.
        if outcome.label == LABEL_FAILED and outcome.fault != FAULT_MODEL:
            continue

        version = (await session.execute(
            select(SiteVersion)
            .where(
                SiteVersion.business_id == business.id,
                SiteVersion.files.isnot(None),
                SiteVersion.created_at <= edit.created_at,
            )
            .order_by(SiteVersion.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if version is None or not (version.files or {}).get("style.css"):
            # Nothing to replay against. Common for the very first messages on a site,
            # which arrived before anything had been built.
            continue

        earlier = [
            row for row in history.get(business.id, [])
            if row.created_at < edit.created_at
        ]
        candidates.append(
            Candidate(outcome, edit, business, version, rebuild_context(earlier))
        )
        if len(candidates) >= limit:
            break

    return candidates


def fixture_from(candidate: Candidate) -> dict:
    business = candidate.business
    return {
        "business": {field: getattr(business, field) for field in BUSINESS_FIELDS},
        "services": [
            {"name": s.name, "price_label": s.price_label, "is_active": s.is_active}
            for s in business.services
        ],
        "media": [{"kind": m.kind, "url": m.url} for m in business.media],
        "files": candidate.version.files,
    }


def case_from(candidate: Candidate, fixture_name: str) -> dict:
    """The assertion the evidence actually supports, and nothing beyond it."""
    outcome, edit = candidate.outcome, candidate.edit
    case: dict = {
        "id": case_id_for(outcome),
        "fixture": fixture_name,
        "message": edit.raw_message,
        "generated": True,
        "occurred_at": outcome.occurred_at.isoformat() if outcome.occurred_at else None,
        "site": candidate.business.name,
    }
    if candidate.context:
        case["context"] = candidate.context

    if outcome.label == LABEL_CLARIFY_LOOP:
        # It asked, was answered, and asked again. A third question is the same failure.
        case["forbid_operation"] = ["clarify"]
        case["note"] = "the bot asked twice without understanding; it must not ask again"

    elif outcome.label == LABEL_REASKED:
        # It ran, and the owner asked for the same thing again. We know the operation it
        # chose did not satisfy them; we do not know which one would have. So the only
        # honest assertion is that it must not land on that same answer.
        if edit.parsed_operation:
            case["forbid_identical"] = edit.parsed_operation
        case["note"] = (
            "this ran without error and the owner asked for the same thing again "
            f"{_gap_phrase(outcome)}; the operation below did not satisfy them"
        )

    else:  # LABEL_FAILED with a model fault
        case["forbid_operation"] = ["clarify"]
        case["note"] = f"failed in production: {(outcome.signature or '').strip()}"

    return case


def _gap_phrase(outcome: EditOutcome) -> str:
    """How long the owner waited before asking again, in words.

    The gap is the strength of the evidence -- a repeat four minutes later is someone
    watching the page reload, an hour later is someone who came back disappointed -- so
    it goes in the note where whoever tightens this case will read it.
    """
    seconds = (outcome.detail or {}).get("gap_seconds")
    if not seconds:
        return "shortly after"
    minutes = round(seconds / 60)
    # Ninety rather than sixty: "71 minutes later" is both truer and easier to picture
    # than "1 hours later", which is what rounding early produced.
    if minutes < 90:
        return f"{minutes} minute{'s' if minutes != 1 else ''} later"
    hours = round(minutes / 60)
    return f"{hours} hour{'s' if hours != 1 else ''} later"


def load_regressions() -> list[dict]:
    if not REGRESSIONS_FILE.exists():
        return []
    return json.loads(REGRESSIONS_FILE.read_text(encoding="utf-8"))


def merge(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Fresh cases win on id, and a case already present is never duplicated.

    Hand-edited fields survive: once someone has tightened a generated case's assertion,
    re-running the promoter must not throw that away. Only cases still carrying
    `generated: true` are replaced.
    """
    by_id = {case["id"]: case for case in existing}
    for case in fresh:
        current = by_id.get(case["id"])
        if current is not None and not current.get("generated"):
            continue
        by_id[case["id"]] = case
    return sorted(by_id.values(), key=lambda case: case.get("occurred_at") or "")


def prune_orphan_fixtures(cases: list[dict]) -> list[str]:
    """Delete generated fixtures no case refers to any more.

    A fixture is a whole site -- roughly 40KB -- and a new one appears every time a site
    gains a version. Left alone, a job that runs weekly fills the repository with
    snapshots nothing reads. Only `regression-*` files are ever considered: the
    hand-written fixtures are not this function's to delete.
    """
    wanted = {case["fixture"] for case in cases}
    removed = []
    for path in FIXTURES_DIR.glob("regression-*.json"):
        if path.stem not in wanted:
            path.unlink()
            removed.append(path.stem)
    return sorted(removed)


def write(cases: list[dict], fixtures: dict[str, dict]) -> tuple[int, int, list[str]]:
    FIXTURES_DIR.mkdir(exist_ok=True)
    written = 0
    for name, fixture in fixtures.items():
        path = FIXTURES_DIR / f"{name}.json"
        payload = json.dumps(fixture, indent=2, ensure_ascii=False, default=str)
        if not path.exists() or path.read_text(encoding="utf-8") != payload:
            path.write_text(payload, encoding="utf-8")
            written += 1
    REGRESSIONS_FILE.write_text(
        json.dumps(cases, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return len(cases), written, prune_orphan_fixtures(cases)
