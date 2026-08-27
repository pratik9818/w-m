"""Clear one owner's recorded token usage, so their free-tier allowance starts again.

The allowance is not a stored number -- `get_tokens_used` sums the `token_usage` rows for
an owner every time it is asked, and compares that to FREE_TIER_TOKEN_LIMIT. So there is
nothing to set back to zero; resetting means deleting the rows that add up.

Those rows are also the only record of what this bot actually costs to run. Every real
per-build and per-edit figure quoted about this project was measured from them, so they
are written out to backups/ first and the file is read back before a single row is
deleted. If the backup cannot be written, nothing is.

    python scripts/reset_quota.py <owner_telegram_id>          # show, then ask
    python scripts/reset_quota.py <owner_telegram_id> --yes    # no prompt
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

from bot_api.config import get_settings
from db.base import init_engine, session_scope
from db.models import TokenUsage
from worker.codegen.quota import FREE_TIER_TOKEN_LIMIT

BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"


def _as_row(usage: TokenUsage) -> dict:
    return {
        "id": str(usage.id),
        "owner_telegram_id": usage.owner_telegram_id,
        "business_id": str(usage.business_id) if usage.business_id else None,
        "model": usage.model,
        "kind": usage.kind,
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "created_at": usage.created_at.isoformat(),
    }


async def reset(owner_telegram_id: int, *, assume_yes: bool) -> int:
    init_engine(get_settings().database_url)

    async with session_scope() as session:
        rows = (await session.execute(
            select(TokenUsage)
            .where(TokenUsage.owner_telegram_id == owner_telegram_id)
            .order_by(TokenUsage.created_at)
        )).scalars().all()

    if not rows:
        print(f"owner {owner_telegram_id} has no usage recorded — nothing to reset.")
        return 0

    total = sum(r.input_tokens + r.output_tokens for r in rows)
    print(f"owner {owner_telegram_id}: {len(rows)} rows, {total:,} tokens "
          f"({total / FREE_TIER_TOKEN_LIMIT * 100:.1f}% of the {FREE_TIER_TOKEN_LIMIT:,} limit)")
    print(f"  {rows[0].created_at:%Y-%m-%d %H:%M} -> {rows[-1].created_at:%Y-%m-%d %H:%M}")

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = BACKUP_DIR / f"token_usage_{owner_telegram_id}_{stamp}.json"
    path.write_text(json.dumps([_as_row(r) for r in rows], indent=2), encoding="utf-8")

    # Read it back before deleting what it exists to protect. A backup nobody has opened
    # is a belief about a file, not a copy of the data.
    restored = json.loads(path.read_text(encoding="utf-8"))
    if len(restored) != len(rows):
        raise SystemExit(f"backup did not round-trip ({len(restored)}/{len(rows)}) — stopping")
    print(f"backed up -> {path}")

    if not assume_yes:
        if input(f"delete these {len(rows)} rows? [y/N] ").strip().lower() not in ("y", "yes"):
            print("left alone.")
            return 0

    async with session_scope() as session:
        result = await session.execute(
            delete(TokenUsage).where(TokenUsage.owner_telegram_id == owner_telegram_id))
        await session.commit()
        deleted = result.rowcount

    async with session_scope() as session:
        remaining = (await session.execute(
            select(func.coalesce(func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens), 0))
            .where(TokenUsage.owner_telegram_id == owner_telegram_id))).scalar_one()

    print(f"deleted {deleted} rows — usage is now {int(remaining):,} tokens "
          f"of {FREE_TIER_TOKEN_LIMIT:,}")
    return deleted


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--yes"]
    if len(args) != 1 or not args[0].isdigit():
        raise SystemExit(__doc__)
    asyncio.run(reset(int(args[0]), assume_yes="--yes" in sys.argv))


if __name__ == "__main__":
    main()
