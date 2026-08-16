"""Standalone codegen test harness for Part 2 — not wired to the bot yet.

Usage:
    python scripts/generate_site.py --business-id <uuid>
    python scripts/generate_site.py --sample plumber
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from bot_api.config import get_settings
from bot_api.services.business_service import get_business_by_id
from db.base import init_engine, session_scope
from worker.codegen.builder import GenerationFailed, build_site, spec_from_business
from worker.codegen.quota import FREE_TIER_TOKEN_LIMIT, QuotaExceeded, get_tokens_used
from worker.codegen.samples import SAMPLES

OUTPUT_DIR = Path(__file__).parent.parent / "generated_output"


def _write_output(name: str, files: dict[str, str]) -> Path:
    out_dir = OUTPUT_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (out_dir / filename).write_text(content, encoding="utf-8")
    return out_dir


def _print_usage(usage: dict, quota_line: str | None = None) -> None:
    total = usage["input_tokens"] + usage["output_tokens"]
    print(
        f"Tokens this call: {usage['input_tokens']} in + {usage['output_tokens']} out "
        f"= {total} ({usage['model']})"
    )
    if quota_line:
        print(quota_line)


async def run_business(business_id: str) -> None:
    settings = get_settings()
    init_engine(settings.database_url)

    async with session_scope() as session:
        business = await get_business_by_id(session, uuid.UUID(business_id))
        if business is None:
            print(f"No business found with id {business_id}", file=sys.stderr)
            sys.exit(1)

        spec = spec_from_business(business)
        try:
            files, usage = await build_site(
                spec,
                session=session,
                owner_telegram_id=business.owner_telegram_id,
                business_id=business.id,
            )
        except QuotaExceeded as exc:
            print(f"Quota exceeded: {exc.used}/{exc.limit} tokens already used for this owner.")
            sys.exit(1)
        except GenerationFailed as exc:
            print(f"Generation failed: {exc}", file=sys.stderr)
            sys.exit(1)

        out_dir = _write_output(business.slug, files)
        total_used = await get_tokens_used(session, business.owner_telegram_id)

    print(f"Wrote {out_dir}")
    _print_usage(usage, f"Owner running total: {total_used}/{FREE_TIER_TOKEN_LIMIT} tokens")


async def run_sample(name: str) -> None:
    if name not in SAMPLES:
        print(f"Unknown sample '{name}'. Available: {', '.join(SAMPLES)}", file=sys.stderr)
        sys.exit(1)

    try:
        files, usage = await build_site(SAMPLES[name])
    except GenerationFailed as exc:
        print(f"Generation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    out_dir = _write_output(name, files)
    print(f"Wrote {out_dir}")
    _print_usage(usage)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the site-generation harness standalone.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--business-id", help="UUID of a real business already saved in Postgres")
    group.add_argument("--sample", help=f"Canned sample spec: {', '.join(SAMPLES)}")
    args = parser.parse_args()

    if args.business_id:
        asyncio.run(run_business(args.business_id))
    else:
        asyncio.run(run_sample(args.sample))


if __name__ == "__main__":
    main()
