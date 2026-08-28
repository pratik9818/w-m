"""Turn labelled production failures into permanent eval cases.

Usage (from the repo root):
    python scripts/promote_failures.py --dry-run       # see what it would add
    python scripts/promote_failures.py --days 30
    python scripts/promote_failures.py                 # last 30 days

Run it after scripts/failure_report.py, which produces the labels this reads.

Makes no model call: it copies stored file snapshots into evals/fixtures/ and writes
evals/regressions.json. Running the resulting cases costs tokens; producing them does not.

Only failures the parser could have prevented are promoted. Code defects are listed at the
end for a hand-written unit test instead -- a parser eval that passes while the executor
is broken is worse than no eval at all.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot_api.config import get_settings  # noqa: E402
from db.base import init_engine, session_scope  # noqa: E402
from evals.promote import (  # noqa: E402
    case_from,
    find_candidates,
    fixture_from,
    fixture_name_for,
    load_regressions,
    merge,
    write,
)
from worker.learning.ledger import failure_rows  # noqa: E402
from worker.learning.signatures import FAULT_CODE  # noqa: E402


async def main(days: int, limit: int, dry_run: bool) -> int:
    init_engine(get_settings().database_url)

    async with session_scope() as session:
        candidates = await find_candidates(session, days=days, limit=limit)
        code_defects = [
            row for row in await failure_rows(session, days=days)
            if row.fault == FAULT_CODE and not row.is_resolved
        ]

        fixtures: dict[str, dict] = {}
        fresh: list[dict] = []
        for candidate in candidates:
            name = fixture_name_for(candidate.business, candidate.version)
            fixtures.setdefault(name, fixture_from(candidate))
            fresh.append(case_from(candidate, name))

    existing = load_regressions()
    known = {case["id"] for case in existing}
    merged = merge(existing, fresh)
    added = [case for case in fresh if case["id"] not in known]

    by_label = Counter(case["id"].rsplit("-", 1)[0] for case in fresh)
    print(f"{len(candidates)} promotable failure(s) in the last {days} days")
    for label, count in sorted(by_label.items()):
        print(f"  {count:>3}  {label}")
    print(f"\n{len(added)} new case(s), {len(merged)} in the corpus, "
          f"{len(fixtures)} fixture(s)")

    for case in added[:10]:
        print(f"\n  + {case['id']}  ({case['site']})")
        print(f"      {' '.join(case['message'].split())[:100]}")
        print(f"      {case['note']}")
    if len(added) > 10:
        print(f"\n  ... and {len(added) - 10} more")

    if dry_run:
        print("\n(dry run -- nothing written)")
        return 0

    total, fixtures_written, pruned = write(merged, fixtures)
    print(f"\nwrote evals/regressions.json ({total} cases) "
          f"and {fixtures_written} fixture file(s)")
    if pruned:
        print(f"pruned {len(pruned)} fixture(s) no case refers to any more")

    if code_defects:
        print("\nNOT promoted -- these are code defects, not parsing mistakes.")
        print("They need a unit test against the executor, not the parser:")
        for row in code_defects:
            print(f"  {row.signature}  ({row.count}x, {row.site_count} site(s))")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="how far back to look")
    parser.add_argument("--limit", type=int, default=50, help="most cases to promote")
    parser.add_argument("--dry-run", action="store_true", help="show without writing")
    args = parser.parse_args()

    sys.exit(asyncio.run(main(args.days, args.limit, args.dry_run)))
