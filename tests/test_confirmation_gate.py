"""Nothing rewrites an owner's site until they have said yes to it.

The bot's edit handler is reached by any free text from a known owner that is not a
command -- that is what makes editing feel like conversation, and it is also what made a
misdirected message dangerous. A note typed into the wrong chat window, a reply meant for
someone else, a half-finished thought sent by accident: each of those read exactly like an
instruction, and each was carried out. There was no step between "the parser produced an
operation" and "the site has been rewritten and republished".

These tests are about that gap. They are deliberately structural -- the handler needs a
database, Redis, Telegram and two model calls to run end to end, and none of those is what
is being checked. What is being checked is that the acting half of the handler cannot be
reached except through the affirmation branch, which is a property of the code's shape.
"""

import ast
import inspect
import textwrap

import pytest

from bot_api.bot.filters import is_affirmation
from bot_api.bot.handlers import edit as edit_handler
from bot_api.bot.handlers import onboarding as onboarding_handler
from bot_api.bot.states.onboarding import OnboardingStates


def _tree(func):
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


def _called_names(func) -> set[str]:
    """Every function name called anywhere inside `func`."""
    names = set()
    for node in ast.walk(_tree(func)):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


# --------------------------------------------------------------- the edit handler

# Calling any of these means the owner's site is about to change.
_ACTING = {"enqueue_generation", "enqueue_rollback", "apply_edit_operation",
           "apply_style_changes", "_apply_operation", "_rebuild_with_layout",
           "_add_a_found_photo"}

# The two things that mean "the owner has just told us to do this specific thing".
# `is_affirmation` is the yes. `_already_asked_layout` is narrower and needs saying: it is
# only true when the bot's own previous turn asked this owner which layout they wanted, so
# the message being acted on is the answer to that question. Requiring a further yes there
# would ask the same thing twice in a row, which is the loop this handler spent two days
# stuck in -- an owner was sent the identical question three times, the third in reply to
# their own answer.
_MEANS_CONSENT = ("is_affirmation", "_already_asked_layout", "_already_asked_picture")


def _acting_calls_with_their_guards(func):
    """Every acting call in `func`, paired with the `if` tests enclosing it."""
    found = []

    class Finder(ast.NodeVisitor):
        def __init__(self):
            self.guards = []

        def visit_If(self, node):
            self.guards.append(node.test)
            self.generic_visit(node)
            self.guards.pop()

        def visit_Call(self, node):
            target = node.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if name in _ACTING:
                found.append((name, [ast.unparse(g) for g in self.guards]))
            self.generic_visit(node)

    Finder().visit(_tree(func))
    return found


def test_the_reading_half_never_acts_unless_the_owner_just_said_so():
    """`catch_all_edit` runs on any message an owner types. Nothing in it may change the
    site except under a guard that means they asked for this, now.

    This is the whole guarantee, stated as directly as it can be. It is checked over the
    syntax tree because the risk is a branch someone adds later and forgets to gate --
    exactly how the ungated version came about -- and a test that drives the paths it
    already knows about would miss that for the same reason the author did.
    """
    unguarded = [
        f"{name} (guards: {' | '.join(guards) or 'none'})"
        for name, guards in _acting_calls_with_their_guards(edit_handler.catch_all_edit)
        if not any(marker in " ".join(guards) for marker in _MEANS_CONSENT)
    ]

    assert not unguarded, (
        "these change the owner's site without them having asked for it in the message "
        "being handled:\n  " + "\n  ".join(unguarded)
    )


def test_acting_is_reachable_only_from_the_affirmation_branch():
    """`_apply_operation` is the acting half. Every call to it must sit under a yes."""
    calls = []

    class Finder(ast.NodeVisitor):
        def __init__(self):
            self.guards = []

        def visit_If(self, node):
            self.guards.append(node.test)
            self.generic_visit(node)
            self.guards.pop()

        def visit_Call(self, node):
            target = node.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if name == "_apply_operation":
                calls.append([ast.unparse(g) for g in self.guards])
            self.generic_visit(node)

    Finder().visit(_tree(edit_handler.catch_all_edit))

    assert calls, "the acting half is never called; the gate would have wedged the bot shut"
    for guards in calls:
        joined = " ".join(guards)
        assert "is_affirmation" in joined, (
            "_apply_operation is called without an affirmation guarding it: " + joined
        )


def test_the_gate_parks_the_operation_rather_than_dropping_it():
    """Asking is only half of it -- a yes has to be able to find what it is answering."""
    source = inspect.getsource(edit_handler.catch_all_edit)
    assert "set_pending_edit" in source
    assert "PENDING_CONFIRM" in source


def test_the_original_request_is_what_gets_recorded_not_the_word_yes():
    """The edit log and the conversation buffer are read by people and by the next model
    call. A build traced back to the message "yes" is traced back to nothing."""
    source = inspect.getsource(edit_handler.catch_all_edit)
    assert '"raw_message": raw_message' in source, (
        "the pending record must carry the request, or the log will show the confirmation"
    )
    apply_source = inspect.getsource(edit_handler.catch_all_edit)
    assert 'pending.get("raw_message")' in apply_source


@pytest.mark.parametrize("op, expected", [
    ({"operation": "patch_site", "instruction": "Make the heading bigger"},
     "make the heading bigger"),
    ({"operation": "set_style", "summary": "Turn the buttons green"},
     "turn the buttons green"),
    ({"operation": "change_layout", "layout": "landing"}, "single landing page"),
    ({"operation": "update_business_info", "phone": "0113 496 0000"}, '"0113 496 0000"'),
    ({"operation": "add_service", "name": "Deep tissue massage", "price_label": "£60"},
     '"Deep tissue massage" (£60)'),
    ({"operation": "remove_service", "name": "Waxing"}, 'remove "Waxing"'),
])
def test_the_question_says_what_will_actually_happen(op, expected):
    """A confirmation that does not name the change is a button, not a question -- an owner
    who cannot see what they are agreeing to will say yes to anything."""
    question = edit_handler._confirmation_message(op, "Rise &amp; Crumb")
    assert expected in question
    assert "yes" in question.lower()


def test_a_long_rewrite_is_summarised_rather_than_quoted_whole():
    """Quoting three paragraphs of about-copy back makes the question unreadable, which is
    its own way of not being answered."""
    op = {"operation": "update_business_info", "about": "We are a bakery. " * 40}
    question = edit_handler._confirmation_message(op, "Rise & Crumb")
    assert "rewrite your about section" in question
    assert "We are a bakery. We are a bakery." not in question


# --------------------------------------------------------------- what counts as a yes

@pytest.mark.parametrize("text", ["yes", "Yes", "YES.", "yep", "ok", "sure", "  yes  ",
                                  "go ahead", "do it", "build it", "y",
                                  # Trailing punctuation is stripped, so this counts. Kept
                                  # as it was rather than tightened: the drafted-copy flow
                                  # has read agreement this way in production for months,
                                  # and the cost of the other reading is one extra message.
                                  "yes?"])
def test_plain_agreement_is_a_yes(text):
    assert is_affirmation(text)


@pytest.mark.parametrize("text", [
    "yes but change the phone number first",
    "no",
    "not yet",
    "yesterday we were closed",
    "make the heading bigger",
    "",
    None,
])
def test_anything_carrying_more_than_agreement_is_not_a_yes(text):
    """The dangerous near-miss is "yes, but ...". Read as a yes it applies the original
    unchanged operation and silently discards the correction riding with it -- which is
    the exact failure the gate exists to prevent, arriving one message later."""
    assert not is_affirmation(text)


# --------------------------------------------------------------- the create flow

def test_building_a_site_waits_for_a_yes_too():
    """A brief is one free-text message that the model turns into a name, a category, a
    layout and the marketing copy. The owner has seen none of that when it is parsed."""
    called = _called_names(onboarding_handler.on_brief)
    assert "enqueue_generation" not in called, (
        "reading the brief queues a build before the owner has seen what was understood"
    )
    assert "create_business_from_spec" not in called

    confirmed = _called_names(onboarding_handler.on_confirm)
    assert "enqueue_generation" in confirmed
    assert "is_affirmation" in confirmed


def test_the_summary_shows_the_owner_what_was_understood():
    op = {
        "name": "Rise & Crumb", "category": "Bakery", "layout": "landing",
        "tagline": "Stone-baked every morning",
        "services": [{"name": "Sourdough"}, {"name": "Celebration cakes"}],
        "phone": "0113 496 0000", "email": "hi@riseandcrumb.co.uk",
    }
    summary = onboarding_handler._brief_summary(op)

    assert "Rise &amp; Crumb" in summary or "Rise & Crumb" in summary
    assert "Bakery" in summary
    assert "one-page landing site" in summary
    assert "Sourdough" in summary and "Celebration cakes" in summary
    assert "0113 496 0000" in summary


def test_a_thin_brief_still_produces_a_readable_summary():
    """Every optional line is genuinely optional; a brief with almost nothing in it must
    not render as a list of blanks."""
    summary = onboarding_handler._brief_summary({"name": "Raj Plumbing"})

    assert "Raj Plumbing" in summary
    assert "None" not in summary
    assert "::" not in summary


def test_the_confirm_state_exists_and_is_distinct():
    """Without its own state the confirmation would be read by the brief handler, and a
    yes would be parsed as a business description."""
    assert OnboardingStates.waiting_confirm != OnboardingStates.waiting_brief


def test_a_correction_keeps_what_the_owner_already_said():
    """Answering the summary with "no, it's a cafe not a bakery" must not throw away the
    original brief -- re-parsing that one line alone would lose everything else."""
    source = inspect.getsource(onboarding_handler.on_brief)
    # The exact call that hands over to the confirmation, not merely the phrase somewhere
    # in the function: the need-more-info branch above it keeps the brief too, and matching
    # that one instead would let this handover lose it silently.
    assert "parsed=op, brief_history=history + [brief]" in source, (
        "the brief must be kept when moving to the confirmation, or a correction is "
        "parsed on its own with everything the owner already said thrown away"
    )
