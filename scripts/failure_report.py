"""Label recent edits and print the failure ledger.

Usage (from the repo root):
    python scripts/failure_report.py                # last 14 days
    python scripts/failure_report.py --days 30
    python scripts/failure_report.py --rebuild      # re-label everything from scratch
    python scripts/failure_report.py --no-label     # print without re-labelling

Reads only. Makes no model call and touches no live site, so it is safe to run at any
time and costs nothing. Run it weekly, or whenever the bot feels like it is misbehaving --
the whole point is that the answer is a table rather than an afternoon in the logs.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot_api.config import get_settings  # noqa: E402
from db.base import init_engine, session_scope  # noqa: E402
from worker.learning.ledger import (  # noqa: E402
    failure_rows,
    health_counts,
    needs_attention,
    render,
)
from worker.learning.outcomes import label_edits  # noqa: E402


async def main(days: int, relabel: bool, rebuild: bool) -> int:
    init_engine(get_settings().database_url)

    async with session_scope() as session:
        if relabel:
            # Labels look back at what came after an edit, so re-derive a window wider
            # than the one being reported: an edit on the edge of the report can only be
            # judged by messages that fall outside it.
            since = None if rebuild else datetime.now(timezone.utc) - timedelta(days=days * 2)
            tally = await label_edits(session, since=since, rebuild=rebuild)
            labelled = sum(tally.values())
            print(f"labelled {labelled} edit(s)\n")

        rows = await failure_rows(session, days=days)
        counts = await health_counts(session, days=days)

    print(render(rows, counts, days))
    # Non-zero only when something needs a person: an unfixed code defect, or a defect
    # that was fixed and came back. A resolved fault showing its own history is the
    # ledger remembering, not the bot breaking, and must not fail a build.
    return 1 if needs_attention(rows) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="window to report on")
    parser.add_argument("--rebuild", action="store_true", help="re-label all history")
    parser.add_argument("--no-label", action="store_true", help="report without re-labelling")
    args = parser.parse_args()

    sys.exit(asyncio.run(main(args.days, not args.no_label, args.rebuild)))
