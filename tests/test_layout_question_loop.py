"""The bot asked one owner the same question three times, and could never have stopped.

Recovered from Redis, their six turns:

  1. "One change i want in bottom ..."            -> applied
  2. "Remove the about service and contact element" -> a fair clarifying question
  3. "Only nav links"                             -> applied, nav links removed
  4. "Remove service as well"                     -> "landing page or four-page site?"
  5. "No only remove service elements from top"   -> the same question again
  6. "Single landing page"                        -> the same question a third time

Turn 6 is the one that proves it was a loop rather than bad luck: the answer to the
question matched the pattern that asks the question. Two separate defects put them there,
and each gets a test here.
"""

import pytest

from bot_api.bot.handlers.edit import LAYOUT_QUESTION, _already_asked_layout
from bot_api.services.edit_ops import is_structural_request, layout_answer


# --------------------------------------------------------------- the loop itself

@pytest.mark.parametrize("answer", [
    "Single landing page",
    "single landing page",
    "a single landing page please",
    "one page",
    "landing page",
])
def test_answering_the_question_does_not_re_ask_it(answer):
    """`\\blanding page\\b` matched the owner's own answer, so replying to the question
    triggered the question. It could not terminate."""
    assert not is_structural_request(answer), (
        f"{answer!r} is an answer to the layout question, not a request to restructure"
    )
    assert layout_answer(answer) == "landing"


@pytest.mark.parametrize("answer,expected", [
    ("four page site", "multipage"),
    ("4 page", "multipage"),
    ("separate pages", "multipage"),
    ("multi-page", "multipage"),
])
def test_the_other_answer_is_understood_too(answer, expected):
    assert layout_answer(answer) == expected


def test_an_ordinary_message_is_not_mistaken_for_an_answer():
    for text in ("make the heading bigger", "remove the contact form", "add my phone number"):
        assert layout_answer(text) is None


# ------------------------------------------------- the false positive that started it

def test_an_edit_that_merely_mentions_the_page_is_not_structural():
    """Turn 4/5. The instruction the model wrote said "remove the Services link from the
    page" -- one page, one link, nothing structural -- and `remove ... page` matched."""
    for instruction in (
        "In the top navigation menu remove the Services link from the page",
        "Remove service as well",
        "No only remove service elements from top",
        "remove the services heading from the page",
    ):
        assert not is_structural_request(instruction), instruction


def test_genuinely_structural_requests_are_still_caught():
    """The guard exists for a real reason: one such request burned 21,867 tokens across
    three calls and changed nothing, because a patch can never delete the file it is
    handed."""
    for instruction in (
        "delete the about page",
        "remove the contact page and the services page",
        "get rid of about.html",
        "keep only one page",
        "add another page for pricing",
        "make it a single landing page",
        "turn it into a four-page site",
        "convert this to one-page",
    ):
        assert is_structural_request(instruction), instruction


# --------------------------------------------------------- never twice in a row

def test_the_question_is_recognised_as_already_asked():
    context = [
        {"raw_message": "Remove service as well", "outcome": {"bot_asked": LAYOUT_QUESTION}},
    ]
    assert _already_asked_layout(context)


def test_an_unrelated_question_does_not_count():
    context = [
        {"raw_message": "make it nicer",
         "outcome": {"bot_asked": "Which heading did you mean?"}},
    ]
    assert not _already_asked_layout(context)


def test_a_question_from_long_ago_does_not_block_a_fresh_one():
    """Only the last couple of turns count -- an owner who genuinely wants to restructure
    a week later must still be able to be asked."""
    context = [
        {"raw_message": "old", "outcome": {"bot_asked": LAYOUT_QUESTION}},
        {"raw_message": "a", "outcome": {"applied": "patch_site", "summary": "x"}},
        {"raw_message": "b", "outcome": {"applied": "patch_site", "summary": "y"}},
    ]
    assert not _already_asked_layout(context)


def test_no_context_is_not_an_already_asked_question():
    assert not _already_asked_layout(None)
    assert not _already_asked_layout([])
