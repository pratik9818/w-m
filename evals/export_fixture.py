"""Freeze a real business and its live files into an eval fixture.

Usage (from the repo root):
    python evals/export_fixture.py --business-id <uuid> --name engineer-portfolio

Fixtures are snapshots on purpose. The parser's behaviour has to be measured against a
site that does not move, or a run that fails tells you nothing about whether the prompt
changed or the site did.
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot_api.config import get_settings  # noqa: E402
from bot_api.services.business_service import get_business_by_id, get_live_files  # noqa: E402
from db.base import init_engine, session_scope  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

BUSINESS_FIELDS = (
    "name", "category", "tagline", "about", "theme", "layout", "phone", "email",
    "address", "hours", "extra_instructions", "slug",
)


async def main(business_id: str, name: str) -> None:
    init_engine(get_settings().database_url)
    async with session_scope() as session:
        business = await get_business_by_id(session, uuid.UUID(business_id))
        if business is None:
            sys.exit(f"No business with id {business_id}")
        files = await get_live_files(session, business)
        if not files:
            sys.exit("That business has no live files to freeze")

        fixture = {
            "business": {field: getattr(business, field) for field in BUSINESS_FIELDS},
            "services": [
                {"name": s.name, "price_label": s.price_label, "is_active": s.is_active}
                for s in business.services
            ],
            "media": [{"kind": m.kind, "url": m.url} for m in business.media],
            "files": files,
        }

    FIXTURES_DIR.mkdir(exist_ok=True)
    path = FIXTURES_DIR / f"{name}.json"
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path} ({len(files)} files, {sum(len(f) for f in files.values()):,} chars)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--name", required=True, help="fixture filename, without .json")
    args = parser.parse_args()
    asyncio.run(main(args.business_id, args.name))
