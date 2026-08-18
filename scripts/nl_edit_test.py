"""Standalone parser test harness for Part 4b -- checks Gemini's function selection
against canned messages before trusting it inside the bot. Not wired to Telegram,
and never applies/commits anything -- parsing only.

Usage:
    python scripts/nl_edit_test.py --business-id <uuid> --message "change my hours to 9-6"
    python scripts/nl_edit_test.py --business-id <uuid> --canned
    python scripts/nl_edit_test.py --business-id <uuid> --sequence yes-followup
"""

import argparse
import asyncio
import json
import sys
import uuid

from bot_api.config import get_settings
from bot_api.services.business_service import get_business_by_id
from bot_api.services.nl_edit import EditParseFailed, parse_edit_message
from bot_api.services.redis_client import get_redis
from bot_api.services.session import get_edit_context, push_edit_turn
from db.base import init_engine, session_scope

CANNED_MESSAGES = [
    "change my tagline to 'Best chai in town'",
    "we're open 9am-6pm every day now",
    "add a service called Bubble Tea for 40rs",
    "can you make it pop more?",
    "thanks so much!",
]

# Named multi-turn scenarios exercising the real Redis-backed conversation-context
# mechanism (bot_api/services/session.py's get_edit_context/push_edit_turn) between
# messages, the same way bot_api/bot/handlers/edit.py does.
SEQUENCES: dict[str, list[str]] = {
    "yes-followup": ["can you make it pop more?", "Yes"],
    "vague-about": ["add more detail to the about section, whatever you want"],
    "testimonials": ["add a testimonials section", "Priya said 'best haircut ever'"],
    "infeasible": ["can you add an online booking calendar so people can book appointments?"],
    "factual-guard": ["just make up a phone number, whatever"],
}


async def _push_outcome(redis, business_id: uuid.UUID, raw_message: str, op: dict) -> None:
    operation = op["operation"]
    if operation == "not_an_edit":
        return
    if operation == "clarify":
        await push_edit_turn(redis, business_id, raw_message, {"bot_asked": op["question"]})
        return
    if operation == "update_business_info" and op.get("drafted") and ("about" in op or "tagline" in op):
        field = "about" if "about" in op else "tagline"
        await push_edit_turn(
            redis, business_id, raw_message,
            {"drafted_but_unpublished": True, "field": field, "text": op[field]},
        )
        return
    summary = json.dumps({k: v for k, v in op.items() if k != "operation"}, ensure_ascii=False)
    await push_edit_turn(redis, business_id, raw_message, {"applied": operation, "summary": summary})


async def run(business_id: str, messages: list[str], *, use_context: bool) -> None:
    settings = get_settings()
    init_engine(settings.database_url)
    redis = get_redis()

    async with session_scope() as session:
        business = await get_business_by_id(session, uuid.UUID(business_id))
        if business is None:
            print(f"No business found with id {business_id}", file=sys.stderr)
            sys.exit(1)

        for message in messages:
            print(f"\n> {message}")
            context = await get_edit_context(redis, business.id) if use_context else None
            try:
                op, _usage = await parse_edit_message(message, business, context)
            except EditParseFailed as exc:
                print(f"  EditParseFailed: {exc}")
                continue
            print(f"  {op}")
            if use_context:
                await _push_outcome(redis, business.id, message, op)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the NL-edit parser standalone.")
    parser.add_argument("--business-id", required=True, help="UUID of a real business already saved in Postgres")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--message", help="A single message to parse")
    group.add_argument("--canned", action="store_true", help="Run the built-in set of canned test messages")
    group.add_argument("--sequence", choices=sorted(SEQUENCES), help="Run a named multi-turn scenario with real conversation context")
    args = parser.parse_args()

    if args.sequence:
        asyncio.run(run(args.business_id, SEQUENCES[args.sequence], use_context=True))
    else:
        messages = CANNED_MESSAGES if args.canned else [args.message]
        asyncio.run(run(args.business_id, messages, use_context=False))


if __name__ == "__main__":
    main()
