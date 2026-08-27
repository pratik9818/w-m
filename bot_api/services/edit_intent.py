"""Work out what the owner actually means, before anything touches the site.

Edits used to go straight from a chat message to one structured operation in a single
call. That call had eleven functions to choose between, ten of which do something and one
of which (`clarify`) asks a question -- so a small model under pressure to pick one
almost always picked an action. A half-understood message became a real edit to a live
site, and the owner found out by looking at the result.

This runs first and does only one thing: read the message against the site as it actually
is, and decide whether it is understood well enough to act on.

  - plan_edit    -- understood; here is the goal and each concrete change it breaks into
  - ask_owner    -- something is genuinely ambiguous; ask, and change nothing
  - not_a_change -- a greeting or a thank-you, not an edit at all

Separating this from choosing the operation matters because the two decisions pull in
opposite directions. "Which function fits?" rewards committing to one; "do I actually
understand this?" rewards admitting when the answer is no. Asked together, the first
question drowns out the second.

The breakdown is not thrown away once made: it is handed to the operation parser as
context, so an instruction covering three things is written knowing about all three
rather than flattening them into whichever one the model noticed first.
"""
import json
import logging

from bot_api.services.llm_client import (
    DailyLimitReached,
    LLMCallFailed,
    call_forced_tool,
)
from bot_api.services.session import render_edit_context
from db.models import Business
from worker.codegen.builder import spec_from_business
from worker.codegen.outline import outline_site

logger = logging.getLogger(__name__)

# Enough to show the owner what will happen without turning the reply into a document.
MAX_CHANGES = 6


class EditNotUnderstood(Exception):
    """The understanding call itself failed -- a technical fault, not an unclear message."""


PLAN_TOOL = {
    "name": "plan_edit",
    "description": (
        "You understand what the owner wants and could point at the thing on the page that "
        "has to change. Use this whenever the request is clear enough to act on."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "one short sentence, in the owner's own terms, of what they want to end "
                    "up with -- not how you will do it"
                ),
            },
            "changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "each separate change the message asks for, one per entry. A message "
                    "asking for three things has three entries. Write each one in plain "
                    "language the owner would recognise, describing what they will see "
                    "differently -- these are shown to them. Never a class name or a "
                    "selector: 'make the main heading bigger', not 'hero-title'."
                ),
            },
        },
        "required": ["goal", "changes"],
    },
}

ASK_TOOL = {
    "name": "ask_owner",
    "description": (
        "Acting on this would mean guessing at something the message does not settle. Ask "
        "the owner instead. Nothing is changed until they answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "the one question to send, in plain language, answerable in a sentence"
                ),
            },
            "unclear": {
                "type": "string",
                "description": "what specifically you could not determine from the message",
            },
        },
        "required": ["question"],
    },
}

NOT_A_CHANGE_TOOL = {
    "name": "not_a_change",
    "description": (
        "The message is not asking for the site to change -- a greeting, a thank-you, or "
        "chat about something else."
    ),
    "parameters": {"type": "object", "properties": {}},
}

TOOLS = [PLAN_TOOL, ASK_TOOL, NOT_A_CHANGE_TOOL]

PROMPT_TEMPLATE = """A small business owner has sent a message about their website. Before
anything is changed, work out whether you actually understand what they want.

Do not decide *how* to make the change here and do not write any HTML or CSS. Decide only
two things: what they are asking for, and whether it is clear enough to act on.

## The business and its site

{spec_json}
{site_outline}{context_section}
## The owner's message

{raw_message}

## How to think about it

1. Read the message against the map of the site above. The map is what is really on the
   page right now -- if the thing they mention is in it, you know where it is.
2. Break the message into the separate changes it is actually asking for. "Make the
   heading bigger and change the button to green" is two changes, not one.
3. Then decide: could someone looking at this page carry every one of those out without
   guessing? If yes, call plan_edit. If no, call ask_owner.

## When to ask

Ask when the message could sensibly be acted on in two different ways, or when it needs a
fact you do not have:

- It names something that is not on the page and you cannot tell what it corresponds to.
- It could mean two different elements -- "the button" where the map shows four buttons.
- It needs a real value you were not given: a phone number, a price, an address, an email,
  opening hours, a link destination.
- It asks for a picture, but no picture was supplied and none is on the site.

## When NOT to ask

Asking has a cost -- the owner has to come back and answer, and a question they think is
obvious reads as the bot not listening. Do not ask when:

- **The request is vague but creative.** "Make it look nicer", "add more detail", "make it
  feel premium", "whatever you think" -- that is the owner handing you the judgement, not
  withholding information. Take it and plan the change.
- **The map already answers you.** If there is exactly one heading, "the heading" is not
  ambiguous.
- **They already answered it** in the recent conversation above.
- **You are only asking permission.** You do not need it -- they asked for the change.
- **The decision is yours to make.** Where a new section goes, what to call it, how much
  bigger "bigger" is, which shade of a colour, what the copy says -- the owner is paying
  you to decide those. Choose the sensible option and say what you chose in the change
  itself ("add a shipping section below the services"). Asking about them reads as being
  unable to do the job.
- **The site has not been built yet.** Then the message is about the site to build, not an
  element on a page that does not exist. Plan it.

One question at most, and only about what actually blocks you. If part of a message is
clear and only one detail is not, and you can make a sensible choice for that detail,
plan the whole thing rather than stopping over the detail.

## Two worked examples

Message: "make the main heading bigger and add a section about shipping"
-> plan_edit. Both are clear. Where the new section goes is yours to decide.
   goal: "make the top stand out more and tell customers about shipping"
   changes: ["make the main heading bigger", "add a shipping section below the services"]

Message: "change the button colour"
-> ask_owner, but only because the map shows more than one button. With a single button on
   the page this would be plan_edit, and the shade would be yours to pick.

Now call exactly one function."""


def _plan_from(op: dict) -> dict:
    """Normalise a tool call into the shape the handler uses."""
    if op["operation"] == "not_a_change":
        return {"kind": "not_a_change"}
    if op["operation"] == "ask_owner":
        question = (op.get("question") or "").strip()
        if not question:
            # A question tool call with no question in it cannot be sent to anyone; treat
            # it as understood-nothing and let the operation parser have its own go.
            return {"kind": "unclear_but_unasked"}
        return {"kind": "ask", "question": question, "unclear": (op.get("unclear") or "").strip()}

    changes = [str(c).strip() for c in (op.get("changes") or []) if str(c).strip()]
    return {
        "kind": "plan",
        "goal": (op.get("goal") or "").strip(),
        "changes": changes[:MAX_CHANGES],
    }


def plan_section(plan: dict | None) -> str:
    """The understanding, rendered for the operation parser's prompt."""
    if not plan or plan.get("kind") != "plan":
        return ""
    lines = ["\n## What this message was already understood to mean\n"]
    if plan.get("goal"):
        lines.append(f"The owner wants: {plan['goal']}")
    if plan.get("changes"):
        lines.append("\nBroken down, they are asking for:")
        lines += [f"- {c}" for c in plan["changes"]]
    lines.append(
        "\nThis was worked out by reading their message against the site, so treat it as "
        "settled. Your operation must cover **every** item listed -- if the list has three "
        "entries, an operation that handles one of them is wrong.\n"
    )
    return "\n".join(lines)


def describe_for_owner(plan: dict) -> str:
    """The breakdown, as the owner sees it before the change is made."""
    goal = plan.get("goal") or "make that change"
    changes = plan.get("changes") or []
    if len(changes) <= 1:
        return f"Got it — {goal}"
    listed = "\n".join(f"  {i}. {c}" for i, c in enumerate(changes, start=1))
    return f"Got it — {goal}\n\nThat's {len(changes)} changes:\n{listed}"


async def understand_edit(
    raw_message: str,
    business: Business,
    context: list[dict] | None = None,
    files: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    """Read the message and return (understanding, usage).

    The understanding is one of:
      {"kind": "plan", "goal": str, "changes": [str]}
      {"kind": "ask", "question": str, "unclear": str}
      {"kind": "not_a_change"}
      {"kind": "unclear_but_unasked"}
    """
    outline = outline_site(files)
    prompt = PROMPT_TEMPLATE.format(
        spec_json=json.dumps(spec_from_business(business), indent=2, ensure_ascii=False),
        site_outline=(
            f"\nWhat is actually on the site right now (the map):\n{outline}\n"
            if outline
            else "\nThis site has not been built yet -- there is no page to point at.\n"
        ),
        context_section=render_edit_context(context),
        raw_message=raw_message,
    )

    try:
        op, usage = await call_forced_tool(prompt, TOOLS)
    except DailyLimitReached:
        raise
    except LLMCallFailed as exc:
        raise EditNotUnderstood(f"Could not read that message: {exc}") from exc

    plan = _plan_from(op)
    logger.info(
        "edit.understood",
        extra={
            "event": "edit.understood",
            "kind": plan["kind"],
            "changes": len(plan.get("changes") or []),
        },
    )
    return plan, usage
