"""The bot's only memory of a conversation is the Redis turn buffer, so anything it says
that the next message could be a reply to has to be written there.

Taken from a real exchange, recovered from Redis while it was still live:

    Owner: "There are no images whole website is empty except header and bottom"
    Bot:   "...where would you like them to come from?"
    Owner: "Single landing page"
    Bot:   "Your site is already built as a single landing page. What would you like
            changed about that?"

The owner was answering a question the bot had put to them -- landing page or four pages,
asked by the structural-request branch of the patch path. That branch replied and returned
without recording anything, so by the next message the bot had no idea it had asked, and
the answer arrived looking like an unprompted remark about the layout.
"""

import ast
import inspect
import re
import textwrap

import pytest

from bot_api.bot.handlers import edit as edit_handler
from bot_api.services.session import (
    _EDIT_CONTEXT_MAX_TURNS,
    _EDIT_CONTEXT_TTL_SECONDS,
    render_edit_context,
)


# These end the conversation rather than continue it -- "pick a site first", "I'm busy",
# "that isn't something I can edit". Recording them would put noise in front of the next
# real message, and none of them is a question the owner can answer.
_NOT_PART_OF_AN_EDIT = re.compile(
    r"Not sure that's something I can help edit|Not sure what you'd like to do|"
    r"already being updated|daily limit|couldn't process that just now|"
    r"has been deleted|Rolling <b>|thinking about that|isn't there any more"
)

# Both halves of the handler: the one that reads a message and asks, and the one that acts
# on a yes. They are checked together because the split between them is an implementation
# detail -- an owner sees one conversation. Naming them individually is deliberate: when
# the acting half was first lifted out of `catch_all_edit`, a guard that knew only about
# `catch_all_edit` went on passing while covering half of what it used to.
_HANDLER_HALVES = ("catch_all_edit", "_apply_operation", "_add_a_found_photo")


def _calls(node, name):
    """Does this statement call `name` anywhere inside it?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            rendered = (
                func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                rendered = f"{func.value.id}.{func.attr}"
            if rendered == name or rendered.endswith(f".{name}"):
                return True
    return False


def _spoken_text(node):
    """The literal text of a reply, for deciding whether it's conversational."""
    return " ".join(
        s.value for s in ast.walk(node) if isinstance(s, ast.Constant) and isinstance(s.value, str)
    )


@pytest.mark.parametrize("handler_name", _HANDLER_HALVES)
def test_every_reply_path_records_a_turn(handler_name):
    """A branch that replies to the owner and then returns must record a turn first.

    Walked over the syntax tree rather than by driving the handler, because the defect is
    precisely a path someone forgot to instrument: a test that exercises the paths it
    knows about would have missed all six of these for the same reason the code did. The
    tree is what makes it exact -- a `push_edit_turn` only counts when it is on the same
    branch as the reply, not merely nearby in the file.
    """
    handler = getattr(edit_handler, handler_name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    problems: list[str] = []

    # `if`/`try` open alternative paths: what happens inside one branch says nothing about
    # the others, so state flows in but never out. `with`/`for` are the same path
    # continuing -- the whole handler body sits inside `async with session_scope()` -- so
    # their state does flow back out.
    BRANCHING = (ast.If, ast.Try)
    PASS_THROUGH = (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)

    def walk(body, replied, recorded, *, root=False):
        for stmt in body:
            if isinstance(stmt, BRANCHING):
                branches = [stmt.body, list(getattr(stmt, "orelse", []) or [])]
                branches += [h.body for h in getattr(stmt, "handlers", []) or []]
                for branch in branches:
                    if branch:
                        walk(branch, replied, recorded)
                for final in getattr(stmt, "finalbody", []) or []:
                    replied, recorded = walk([final], replied, recorded)
                continue
            if isinstance(stmt, PASS_THROUGH):
                replied, recorded = walk(stmt.body, replied, recorded)
                continue
            if isinstance(stmt, ast.Return):
                if replied and not recorded:
                    problems.append(f"line {stmt.lineno}: {replied[:80]}")
                return replied, recorded
            if _calls(stmt, "push_edit_turn"):
                recorded = True
            elif _calls(stmt, "answer"):
                text = _spoken_text(stmt)
                if not _NOT_PART_OF_AN_EDIT.search(text):
                    # A reply whose words are built at runtime still needs recording, and
                    # an empty string here would read as "nothing was said".
                    replied = text or "(reply assembled at runtime)"
        if root and replied and not recorded:
            problems.append(f"fall-through: {replied[:80]}")
        return replied, recorded

    walk(tree.body[0].body, None, False, root=True)

    assert not problems, (
        "these replies leave no trace in the conversation buffer, so the owner's next "
        "message is read as though they never happened:\n  " + "\n  ".join(problems)
    )


def test_an_unanswered_question_is_put_in_front_of_the_model():
    """The live failure: the question was in the transcript, and got read past anyway."""
    rendered = render_edit_context([
        {"raw_message": "there are no images on my site",
         "outcome": {"bot_asked": "Where should the images come from?"}},
        {"raw_message": "make it one page",
         "outcome": {"bot_asked": "Do you want a single landing page or a four-page site?"}},
    ])
    # Not merely present -- present *last*, after the transcript, where it cannot be
    # skimmed past as one history line among several.
    assert rendered.rstrip().endswith("Read the new message as that answer first.")
    assert "waiting on an answer to your own question" in rendered
    assert "single landing page or a four-page site" in rendered


def test_a_settled_conversation_does_not_claim_to_be_waiting():
    rendered = render_edit_context([
        {"raw_message": "make the heading bigger",
         "outcome": {"applied": "set_style", "summary": "made the heading bigger"}},
    ])
    assert "waiting on an answer" not in rendered


def test_the_renderer_no_longer_tells_the_model_to_disregard_history():
    """The old wording said to act directly on any self-contained instruction. "Single
    landing page" reads as exactly that, which is how an answer to the bot's own question
    got treated as a new request."""
    rendered = render_edit_context([
        {"raw_message": "hi", "outcome": {"bot_asked": "What would you like to change?"}},
    ])
    assert "act on it directly instead" not in rendered
    assert "part of this exchange" in rendered


def test_the_window_outlasts_a_build_and_a_few_changes_in_a_row():
    """Ten minutes expired mid-conversation while a build the owner was waiting on ran;
    three turns dropped the start of an exchange after one question and one answer."""
    assert _EDIT_CONTEXT_TTL_SECONDS >= 3600
    assert _EDIT_CONTEXT_MAX_TURNS >= 6
