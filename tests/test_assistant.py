"""The bot talking to someone who does not know what a website is.

Everything else in this bot assumes the owner is asking for a change. They mostly are not.
They are asking where their site is, whether it worked, what they have left, or what any of
this means -- and every one of those used to hit the same reply:

    "Not sure that's something I can help edit! ... Use /mysites to switch sites or
     /newsite to build another."

That is addressed to someone who already knows what this bot is and what a slash command
is. The people it is for know neither. They know how to ask a question, which is what they
did, and they were told they had asked wrongly.

The tests here are mostly about two properties. Facts must come from the database and never
from the model, because a confidently wrong web address is worse than no answer. And no
path may end without telling the owner what to do next.
"""

import inspect

import pytest

from bot_api.services import assistant
from bot_api.services.assistant import (
    CAPABILITIES,
    FALLBACK_REPLY,
    answer_from_facts,
    looks_like_a_question,
    render_facts,
)


def _facts(sites=(), used=500_000, limit=4_000_000):
    return {
        "sites": list(sites),
        "tokens_used": used,
        "tokens_limit": limit,
        "tokens_remaining": max(limit - used, 0),
        "changes_left": max(limit - used, 0) // 15_000,
    }


LIVE_SITE = {"name": "Rise & Crumb", "url": "https://rise-and-crumb.pages.dev",
             "status": "live", "layout": "landing", "is_active": True}
BUILDING_SITE = {"name": "Raj Plumbing", "url": None, "status": "generating",
                 "layout": "multipage", "is_active": False}


# --------------------------------------------------- answered without a model call

@pytest.mark.parametrize("question", [
    "what's my link?",
    "whats my link",
    "where is my website",
    "can you give me the address",
    "show me my site",
    "is my site live?",
])
def test_asking_for_the_link_is_answered_from_the_database(question):
    reply = answer_from_facts(question, _facts([LIVE_SITE]))
    assert reply is not None, "this is a column lookup; it must not need a model"
    assert "https://rise-and-crumb.pages.dev" in reply


@pytest.mark.parametrize("question", [
    "how many tokens do i have",
    "how much have i used?",
    "what's my allowance",
    "how many changes are left?",
])
def test_asking_about_the_allowance_is_answered_from_the_database(question):
    reply = answer_from_facts(question, _facts(used=1_000_000, limit=4_000_000))
    assert reply is not None
    assert "3,000,000" in reply
    assert "200" in reply, "the figure they can act on is changes, not tokens"


@pytest.mark.parametrize("question", ["how many websites do i have?", "list my sites",
                                      "what are my sites"])
def test_asking_how_many_sites_lists_them(question):
    reply = answer_from_facts(question, _facts([LIVE_SITE, BUILDING_SITE]))
    assert "Rise & Crumb" in reply
    assert "Raj Plumbing" in reply
    assert "2 sites" in reply


@pytest.mark.parametrize("question", ["what can you do?", "how does this work",
                                      "who are you", "help"])
def test_asking_what_the_bot_does_gets_the_capabilities(question):
    assert answer_from_facts(question, _facts([LIVE_SITE])) == CAPABILITIES


def test_the_capabilities_are_written_as_things_to_say_not_commands():
    """A list of slash commands is a list of things they will not type."""
    assert "/newsite" not in CAPABILITIES
    assert "/mysites" not in CAPABILITIES
    assert "I want a website" in CAPABILITIES


# --------------------------------------------------- not mistaking edits for questions

@pytest.mark.parametrize("instruction", [
    "make the link bigger",
    "change the address on my contact page",
    "remove the token section",
    "put my site name at the top",
])
def test_an_edit_that_mentions_these_words_is_not_hijacked(instruction):
    """"Make the link bigger" is a change to a page and mentions a link. Answering it with
    "here's your web address" would be confidently unhelpful."""
    assert not looks_like_a_question(instruction)
    assert answer_from_facts(instruction, _facts([LIVE_SITE])) is None


@pytest.mark.parametrize("question", ["what's my link?", "how many sites do i have",
                                      "what can you do"])
def test_a_real_question_takes_the_free_path(question):
    assert looks_like_a_question(question)


def test_something_the_facts_cannot_answer_falls_through_to_the_model():
    """"Why does my site look different on my phone?" is a real question with no stored
    answer. It must reach the model rather than be guessed at."""
    assert answer_from_facts("why does my site look different on my phone?",
                             _facts([LIVE_SITE])) is None


# --------------------------------------------------- never a dead end

def test_a_brand_new_owner_asking_for_a_link_is_taught_what_to_do():
    """Where someone lands when they open the bot and type before reading anything."""
    reply = answer_from_facts("where is my website?", _facts([]))

    assert "haven't got a website yet" in reply
    # Not "use /newsite" -- the thing they can actually type, with an example.
    assert "/newsite" not in reply
    assert "tell me about your business" in reply.lower()
    assert "bakery" in reply.lower(), "an example is what makes the instruction usable"


def test_a_site_still_building_is_explained_rather_than_reported_as_missing():
    reply = answer_from_facts("is my site ready?", _facts([BUILDING_SITE]))
    assert "still working on it" in reply
    assert "message you here" in reply


def test_an_exhausted_allowance_says_what_happens_next():
    reply = answer_from_facts("how many tokens left?", _facts(used=4_000_000, limit=4_000_000))
    assert "used your whole allowance" in reply
    assert "let me know" in reply.lower()


def test_the_fallback_still_leaves_a_next_step():
    """Even when the model call itself fails, there is no dead end -- that is the whole
    point of the module, and a broken call is not a reason to reintroduce one."""
    assert "tell me what it is" in FALLBACK_REPLY.lower()


# --------------------------------------------------- facts handed over, never recalled

def test_the_facts_given_to_the_model_carry_the_real_values():
    rendered = render_facts(_facts([LIVE_SITE, BUILDING_SITE], used=1_000_000))

    assert "https://rise-and-crumb.pages.dev" in rendered
    assert "Raj Plumbing" in rendered
    assert "no address yet" in rendered, "a site with no link must be described, not omitted"
    assert "3,000,000 left" in rendered
    assert "currently editing" in rendered


def test_no_sites_is_stated_plainly_rather_than_left_blank():
    """An empty facts block invites the model to fill the gap from imagination."""
    assert "no websites yet" in render_facts(_facts([]))


def test_the_prompt_forbids_inventing_facts():
    prompt = assistant._PROMPT
    assert "Only state facts from the section above" in prompt
    assert "Never invent a web address" in prompt


def test_the_prompt_bans_the_jargon_these_owners_will_not_know():
    prompt = assistant._PROMPT.lower()
    for word in ("deploy", "html", "css", "dns"):
        assert word in prompt, f"{word} should be named as jargon to avoid"
    assert "no jargon" in prompt


def test_the_prompt_requires_a_next_step():
    assert "Never leave them stuck" in assistant._PROMPT


def test_answering_a_question_does_not_pay_for_deliberation():
    """This is conversation, not code. The expensive thinking belongs to writing a site."""
    source = inspect.getsource(assistant.answer_question)
    assert "reduced_reasoning=True" in source


@pytest.mark.asyncio
async def test_a_failed_model_call_still_answers(monkeypatch):
    from bot_api.services.llm_client import LLMCallFailed

    async def boom(*a, **k):
        raise LLMCallFailed("model unavailable")

    monkeypatch.setattr(assistant, "call_plain_completion", boom)
    reply, usage = await assistant.answer_or_fallback("why is my site slow?", _facts([LIVE_SITE]))

    assert reply == FALLBACK_REPLY
    assert usage is None


@pytest.mark.asyncio
async def test_the_facts_actually_reach_the_assembled_prompt(monkeypatch):
    """The load-bearing property of this whole module, and the one that is easiest to lose:
    `render_facts` can be perfect and the prompt can still go out without it. Checked on the
    string that is really sent, not on the pieces it is built from."""
    seen = {}

    async def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        return "Here it is.", {"model": "m", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(assistant, "call_plain_completion", fake_call)
    await assistant.answer_question("where is it?", _facts([LIVE_SITE], used=1_000_000))

    assert "https://rise-and-crumb.pages.dev" in seen["prompt"], (
        "the model was asked about a site whose address it was never given, which is how "
        "an invented web address reaches an owner"
    )
    assert "Rise & Crumb" in seen["prompt"]
    assert "3,000,000 left" in seen["prompt"]
    assert "where is it?" in seen["prompt"]


@pytest.mark.asyncio
async def test_the_conversation_so_far_is_given_to_the_model(monkeypatch):
    """"How many pages does it have?" then "make that one bigger" is one conversation."""
    seen = {}

    async def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        return "Sure — here's how.", {"model": "m", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(assistant, "call_plain_completion", fake_call)
    await assistant.answer_question(
        "and how do i change that?", _facts([LIVE_SITE]),
        [{"raw_message": "how many pages do i have?",
          "outcome": {"bot_said": "Four: home, about, services and contact."}}],
    )

    assert "how many pages do i have?" in seen["prompt"]
    assert "Four: home, about" in seen["prompt"]


@pytest.mark.asyncio
async def test_the_reply_is_capped_to_something_readable_on_a_phone(monkeypatch):
    async def fake_call(prompt, **kw):
        return "x" * 5000, {"model": "m", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(assistant, "call_plain_completion", fake_call)
    reply, _ = await assistant.answer_question("tell me everything", _facts([LIVE_SITE]))

    assert len(reply) <= assistant.MAX_REPLY_CHARS


# --------------------------------------------------- the handler no longer gives up

def test_the_edit_handler_has_no_dead_ends_left():
    from bot_api.bot.handlers import edit as edit_handler

    source = inspect.getsource(edit_handler)
    assert "Not sure that's something I can help edit" not in source
    assert "Not sure what you'd like to do" not in source


def test_every_place_that_used_to_give_up_now_answers():
    from bot_api.bot.handlers import edit as edit_handler

    source = inspect.getsource(edit_handler.catch_all_edit)
    # The three: no site at all, the understanding step saying it is not a change, and the
    # operation parser saying the same.
    assert source.count("_answer_as_assistant") >= 3
