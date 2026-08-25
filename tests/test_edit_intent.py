"""An edit is understood before it is acted on, and a doubt becomes a question.

The failure this exists to stop: a message the bot only half understood became a real
change to a live site, and the owner found out by looking at the result. Understanding is
now a separate decision from choosing an operation, taken first, and allowed to stop
everything by asking instead.
"""

import pytest

from bot_api.services import edit_intent
from bot_api.services.edit_intent import (
    describe_for_owner,
    plan_section,
    understand_edit,
)


class FakeBusiness:
    id = "b1"
    name = "Page Turner Books"
    category = "Book shop"
    tagline = "Books for everyone"
    about = None
    theme = "classic"
    layout = "landing"
    phone = None
    email = None
    address = None
    hours = None
    extra_instructions = None
    services = []
    media = []


@pytest.fixture
def tool_call(monkeypatch):
    """Answer the understanding call with one canned tool call; record the prompt."""
    seen = {}

    def install(op):
        async def fake(prompt, tools):
            seen["prompt"] = prompt
            seen["tools"] = [t["name"] for t in tools]
            return op, {"model": "stub", "input_tokens": 10, "output_tokens": 5}

        monkeypatch.setattr(edit_intent, "call_forced_tool", fake)

    seen["install"] = install
    return seen


@pytest.mark.asyncio
async def test_a_clear_request_is_broken_into_its_separate_changes(tool_call):
    tool_call["install"](
        {
            "operation": "plan_edit",
            "goal": "make the top of the page stand out more",
            "changes": ["enlarge the hero heading", "change the main button to green"],
        }
    )
    plan, usage = await understand_edit("make the heading bigger and the button green", FakeBusiness())
    assert plan["kind"] == "plan"
    assert plan["changes"] == ["enlarge the hero heading", "change the main button to green"]
    assert usage["input_tokens"] == 10


@pytest.mark.asyncio
async def test_an_ambiguous_request_becomes_a_question_and_no_plan(tool_call):
    tool_call["install"](
        {
            "operation": "ask_owner",
            "question": "Which button did you mean — 'Buy now' or 'Contact us'?",
            "unclear": "two buttons on the page",
        }
    )
    plan, _ = await understand_edit("make the button green", FakeBusiness())
    assert plan["kind"] == "ask"
    assert plan["question"].startswith("Which button")
    # Nothing for the operation parser to act on.
    assert plan_section(plan) == ""


@pytest.mark.asyncio
async def test_chit_chat_is_recognised_without_a_second_call(tool_call):
    tool_call["install"]({"operation": "not_a_change"})
    plan, _ = await understand_edit("thanks!", FakeBusiness())
    assert plan["kind"] == "not_a_change"


@pytest.mark.asyncio
async def test_a_question_tool_call_with_no_question_does_not_stall_the_edit(tool_call):
    # An empty question cannot be sent to anyone. Falling through to the operation parser
    # is better than replying with a blank message.
    tool_call["install"]({"operation": "ask_owner", "question": "   "})
    plan, _ = await understand_edit("make it nicer", FakeBusiness())
    assert plan["kind"] == "unclear_but_unasked"


@pytest.mark.asyncio
async def test_the_understanding_step_is_told_the_site_was_never_built(tool_call):
    tool_call["install"]({"operation": "plan_edit", "goal": "g", "changes": ["c"]})
    await understand_edit("add photos", FakeBusiness(), files=None)
    assert "has not been built yet" in tool_call["prompt"]


@pytest.mark.asyncio
async def test_it_only_ever_chooses_between_understanding_outcomes(tool_call):
    tool_call["install"]({"operation": "plan_edit", "goal": "g", "changes": ["c"]})
    await understand_edit("make it nicer", FakeBusiness())
    # No action tools here at all: this call cannot edit anything, only decide.
    assert set(tool_call["tools"]) == {"plan_edit", "ask_owner", "not_a_change"}


@pytest.mark.asyncio
async def test_a_broken_understanding_call_does_not_block_the_edit(monkeypatch):
    async def boom(prompt, tools):
        raise edit_intent.OpenRouterCallFailed("provider down")

    monkeypatch.setattr(edit_intent, "call_forced_tool", boom)
    with pytest.raises(edit_intent.EditNotUnderstood):
        await understand_edit("make it green", FakeBusiness())


@pytest.mark.asyncio
async def test_the_daily_limit_is_not_disguised_as_a_misunderstanding(monkeypatch):
    async def capped(prompt, tools):
        raise edit_intent.DailyLimitReached("per-day cap")

    monkeypatch.setattr(edit_intent, "call_forced_tool", capped)
    with pytest.raises(edit_intent.DailyLimitReached):
        await understand_edit("make it green", FakeBusiness())


# ------------------------------------------------------------------ handing it onward

def test_the_breakdown_reaches_the_operation_parser():
    section = plan_section(
        {"kind": "plan", "goal": "smarten up the top", "changes": ["bigger heading", "green button"]}
    )
    assert "smarten up the top" in section
    assert "- bigger heading" in section and "- green button" in section
    # The parser can only emit one operation, so it has to be told to cover all of them.
    assert "every" in section


def test_the_owner_sees_a_numbered_breakdown_for_a_multi_part_request():
    text = describe_for_owner(
        {"kind": "plan", "goal": "smarten up the top", "changes": ["bigger heading", "green button"]}
    )
    assert "2 changes" in text
    assert "1. bigger heading" in text and "2. green button" in text


def test_a_single_change_is_not_padded_into_a_list():
    text = describe_for_owner({"kind": "plan", "goal": "make the heading bigger", "changes": ["x"]})
    assert "changes:" not in text
    assert text == "Got it — make the heading bigger"
