"""Standalone parser test harness for Part 4b -- checks Gemini's function selection
against canned messages before trusting it inside the bot. Not wired to Telegram.

Usage:
    python scripts/nl_edit_test.py --business-id <uuid> --message "change my hours to 9-6"
    python scripts/nl_edit_test.py --business-id <uuid> --canned
"""

import argparse
import asyncio
import sys
import uuid

from bot_api.config import get_settings
from bot_api.services.business_service import get_business_by_id
from bot_api.services.nl_edit import EditParseFailed, parse_edit_message
from db.base import init_engine, session_scope

CANNED_MESSAGES = [
    "change my tagline to 'Best chai in town'",
    "we're open 9am-6pm every day now",
    "add a service called Bubble Tea for 40rs",
    "can you make it pop more?",
    "thanks so much!",
]


async def run(business_id: str, messages: list[str]) -> None:
    settings = get_settings()
    init_engine(settings.database_url)

    async with session_scope() as session:
        business = await get_business_by_id(session, uuid.UUID(business_id))
        if business is None:
            print(f"No business found with id {business_id}", file=sys.stderr)
            sys.exit(1)

        for message in messages:
            print(f"\n> {message}")
            try:
                op = await parse_edit_message(message, business)
            except EditParseFailed as exc:
                print(f"  EditParseFailed: {exc}")
                continue
            print(f"  {op}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the NL-edit parser standalone.")
    parser.add_argument("--business-id", required=True, help="UUID of a real business already saved in Postgres")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--message", help="A single message to parse")
    group.add_argument("--canned", action="store_true", help="Run the built-in set of canned test messages")
    args = parser.parse_args()

    messages = CANNED_MESSAGES if args.canned else [args.message]
    asyncio.run(run(args.business_id, messages))


if __name__ == "__main__":
    main()
