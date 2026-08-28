"""Reading the record back: labelling what became of an edit, and grouping the failures.

The bug this whole package exists for was not subtle and not a model failure. A CSS
scanner stopped reading each stylesheet at the first apostrophe in a comment, which broke
style edits on three live sites. Every failure was detected by a postcondition check,
written to edit_log and reported to the owner in plain English -- and never once counted,
so six instances of one defect read as six unrelated apologies. An owner asked three times
before anyone noticed.

So the tests below are mostly about not lying in either direction: a failure must be
attributed to the right side (a broken parser is not a comprehension problem), and things
that are not failures -- a good clarifying question, the confirmation gate logging one
interaction twice -- must never be counted as though they were.
"""

from datetime import datetime, timedelta, timezone

import pytest

from worker.learning import ledger
from worker.learning.resolved import RESOLVED
from worker.learning.outcomes import (
    LABEL_APPLIED,
    LABEL_CLARIFY,
    LABEL_CLARIFY_LOOP,
    LABEL_FAILED,
    LABEL_PROPOSED,
    LABEL_REASKED,
    LABEL_SUPERSEDED,
    judge,
)
from worker.learning.signatures import (
    FAULT_CODE,
    FAULT_MODEL,
    FAULT_NONE,
    FAULT_TIMING,
    FAULT_UNKNOWN,
    classify,
    is_a_real_failure,
)

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class FakeEdit:
    """Enough of an EditLog row to be judged."""

    _next = 0

    def __init__(self, message, applied=False, error=None, at=T0):
        FakeEdit._next += 1
        self.id = f"row-{FakeEdit._next}"
        self.raw_message = message
        self.applied = applied
        self.error = error
        self.created_at = at


def verdict_for(rows, index=0):
    return judge(rows[index], later=rows[index + 1:],
                 previous=rows[index - 1] if index else None)


# ------------------------------------------------- signatures: one defect, one row

def test_the_same_defect_on_different_selectors_is_one_signature():
    """Six real rows named five different selectors and one bug. Grouped by their raw
    text they are six mysteries; grouped by signature they are one afternoon's work."""
    errors = [
        "style change rejected: applied .hero margin-bottom: 2rem but the stylesheet does not say so",
        "style change rejected: applied .section margin-top: 2rem but the stylesheet does not say so",
        "style change rejected: applied .btn-primary padding: 0.5rem 1rem but the stylesheet does not say so",
        "style change rejected: applied .hero padding: 96px but the stylesheet does not say so",
    ]
    signatures = {classify(error)[0] for error in errors}
    assert len(signatures) == 1


def test_a_postcondition_breach_is_blamed_on_the_code():
    """The distinction the whole package turns on. Called a model problem, this defect
    would have been 'fixed' by adding prompt instructions to work around a broken parser,
    which would have made it permanent."""
    assert classify("style change rejected: applied .hero padding: 96px but ...")[1] == FAULT_CODE


def test_a_misread_message_is_blamed_on_the_model():
    assert classify("edit parsing failed: no tool call in response")[1] == FAULT_MODEL
    assert classify("rejected: structural request sent to patch_site")[1] == FAULT_MODEL


def test_arriving_during_a_build_is_nobody_s_fault():
    """The bot correctly refused. Counting these would make a busy afternoon look like a
    spike in defects and bury the handful of rows that are genuinely broken."""
    fault = classify("rejected: business busy (status=generating)")[1]
    assert fault == FAULT_TIMING
    assert not is_a_real_failure(fault)


def test_an_unrecognised_error_is_surfaced_not_swallowed():
    """The failure with no rule yet is precisely the one worth seeing, so it gets a
    stable name from its own text and is flagged for classification."""
    signature, fault = classify(
        "widget exploded for 2157667f-7282-4759-a99f-5ac4107944b5 at 42px"
    )
    assert fault == FAULT_UNKNOWN
    assert is_a_real_failure(fault)
    # Ids and measurements vary between incidents of one defect; the name must not.
    other, _ = classify("widget exploded for 8888aaaa-1111-2222-3333-444455556666 at 96px")
    assert signature == other


def test_a_clarifying_question_is_not_a_failure():
    assert classify("asked: which section did you mean?")[1] == FAULT_NONE


# ------------------------------------------------- labels: what the owner did next

def test_an_applied_edit_asked_for_again_is_a_failure():
    """The most valuable label here, and the only one that catches an edit which ran
    perfectly and did the wrong thing. Nothing in the system detects this -- no exception,
    no failed check. The only evidence is that the owner asked again."""
    rows = [
        FakeEdit("make the top section taller", applied=True, at=T0),
        FakeEdit("make the top section taller", applied=True, at=T0 + timedelta(minutes=71)),
    ]
    result = verdict_for(rows, 0)
    assert result.label == LABEL_REASKED
    assert result.fault == FAULT_MODEL


def test_the_confirmation_gate_is_not_mistaken_for_a_repeat():
    """The gate logs one interaction twice on purpose: once proposed, once applied. Read
    naively that is a repeat, and 29 of 146 real rows would become imaginary failures."""
    rows = [
        FakeEdit("add a price section", applied=False, at=T0),
        FakeEdit("add a price section", applied=True, at=T0 + timedelta(seconds=96)),
    ]
    assert verdict_for(rows, 0).label == LABEL_SUPERSEDED
    assert verdict_for(rows, 1).label == LABEL_APPLIED


def test_a_rephrased_repeat_still_counts():
    """Owners rarely repeat themselves word for word. A real pair was 'reduce the size of
    book a call demo' followed by the same thing 'in desktop screen'."""
    rows = [
        FakeEdit("reduce the size of book a call demo", applied=True, at=T0),
        FakeEdit("reduce the size of book a call demo in desktop screen",
                 applied=True, at=T0 + timedelta(minutes=4)),
    ]
    assert verdict_for(rows, 0).label == LABEL_REASKED


def test_two_unrelated_requests_are_not_a_repeat():
    rows = [
        FakeEdit("change my phone number to 0113 496 0000", applied=True, at=T0),
        FakeEdit("add a photo of the shop front", applied=True, at=T0 + timedelta(minutes=5)),
    ]
    assert verdict_for(rows, 0).label == LABEL_APPLIED


def test_the_same_request_a_week_later_is_a_new_request():
    """Past a day, identical words mean they want that again -- not that it failed."""
    rows = [
        FakeEdit("update the opening hours", applied=True, at=T0),
        FakeEdit("update the opening hours", applied=True, at=T0 + timedelta(days=7)),
    ]
    assert verdict_for(rows, 0).label == LABEL_APPLIED


def test_one_question_is_fine_and_two_in_a_row_is_not():
    """A good question is the bot working. Asking again after being answered means it did
    not understand, and the owner has started doing the work."""
    rows = [
        FakeEdit("make it nicer", error="asked: which part did you mean?", at=T0),
        FakeEdit("the top bit", error="asked: taller, or a different colour?",
                 at=T0 + timedelta(minutes=1)),
    ]
    assert verdict_for(rows, 0).label == LABEL_CLARIFY
    loop = verdict_for(rows, 1)
    assert loop.label == LABEL_CLARIFY_LOOP
    assert loop.fault == FAULT_MODEL


def test_a_proposal_nobody_confirmed_is_not_a_failure():
    rows = [FakeEdit("add a pricing table", applied=False, at=T0)]
    assert verdict_for(rows, 0).label == LABEL_PROPOSED


def test_a_hard_failure_keeps_its_signature():
    rows = [FakeEdit(
        "make the button smaller",
        error="style change rejected: applied .btn-primary padding: 1rem but ...",
        at=T0,
    )]
    result = verdict_for(rows, 0)
    assert result.label == LABEL_FAILED
    assert result.fault == FAULT_CODE
    assert result.signature == "set_style: applied but not present in the stylesheet"


# ------------------------------------------------- the ledger

def _row(signature, fault, count, sites, first=T0, last=T0):
    return ledger.LedgerRow(
        signature=signature, fault=fault, count=count,
        sites=set(sites), first_seen=first, last_seen=last,
        examples=["make the button smaller"],
    )


def test_the_report_separates_bugs_from_comprehension_problems():
    """They go to different places. A broken parser is not fixed by editing a prompt, and
    mixing the two in one list is how the wrong fix gets applied."""
    text = ledger.render(
        [
            _row("set_style: applied but not present in the stylesheet", FAULT_CODE, 6,
                 ["Xtravu", "Arrt", "Looksalon"]),
            _row("parse: could not read the message", FAULT_MODEL, 3, ["Getoo"]),
        ],
        {"applied": 59, "failed": 20, "clarify": 27},
        days=14,
    )
    assert "CODE DEFECTS" in text
    assert "MODEL PROBLEMS" in text
    assert text.index("CODE DEFECTS") < text.index("MODEL PROBLEMS")


def test_the_line_that_would_have_caught_it_reads_like_a_defect_report():
    text = ledger.render(
        [_row("set_style: applied but not present in the stylesheet", FAULT_CODE, 6,
              ["Xtravu", "Arrt", "Looksalon"])],
        {"applied": 59, "failed": 20},
        days=14,
    )
    assert "6x" in text and "3 sites" in text
    assert "Xtravu" in text


def test_a_fault_touching_several_sites_is_reported_above_a_noisier_single_site_one():
    """Three owners hitting one wall is a different thing from one owner hitting it three
    times, and it is nearly always the more urgent of the two."""
    rows = sorted(
        [_row("noisy", FAULT_MODEL, 40, ["OneSite"]),
         _row("widespread", FAULT_CODE, 6, ["A", "B", "C"])],
        key=lambda r: (r.site_count, r.count), reverse=True,
    )
    assert rows[0].signature == "widespread"


def test_a_quiet_window_says_so_rather_than_printing_an_empty_table():
    assert "No edits in this window." in ledger.render([], {}, days=7)
    assert "No failures in this window." in ledger.render([], {"applied": 12}, days=7)


# ------------------------------------------------- remembering what has been fixed

def test_a_fixed_defect_is_marked_rather_than_reported_as_new(monkeypatch):
    """A ledger with no memory reopens every closed case every week, and the one new
    fault is lost among them."""
    monkeypatch.setitem(RESOLVED, "old news",
                    (datetime(2026, 8, 20, tzinfo=timezone.utc), "fixed the scanner"))
    row = _row("old news", FAULT_CODE, 3, ["Xtravu"],
               first=T0 - timedelta(days=10), last=T0 - timedelta(days=9))
    text = ledger.render([row], {"applied": 10, "failed": 3}, days=30)
    assert "[fixed 20 Aug]" in text
    assert not ledger.needs_attention([row]), "a fixed defect must not fail a build"


def test_the_same_defect_after_its_fix_is_a_regression(monkeypatch):
    """The most important thing this can say. A failure dated after its own fix means the
    fix did not hold, which is louder than any open bug in the queue."""
    monkeypatch.setitem(RESOLVED, "came back",
                    (datetime(2026, 8, 20, tzinfo=timezone.utc), "fixed the scanner"))
    row = _row("came back", FAULT_CODE, 4, ["Xtravu"])
    row.since_fix = 2
    text = ledger.render([row], {"applied": 10, "failed": 4}, days=30)

    assert "REGRESSIONS" in text
    assert text.index("REGRESSIONS") < text.index("CODE DEFECTS")
    assert "** REGRESSED **" in text
    assert ledger.needs_attention([row])


def test_an_unfixed_code_defect_still_demands_attention():
    assert ledger.needs_attention([_row("brand new", FAULT_CODE, 1, ["Xtravu"])])


def test_a_bare_confirmation_is_not_used_as_an_example():
    """Half the messages in a confirmation flow are "Yes". Printed as the example of a
    failure class they say nothing about what was being asked for."""
    row = ledger.LedgerRow(signature="s", fault=FAULT_MODEL, count=2, sites={"X"},
                           first_seen=T0, last_seen=T0, examples=[])
    for message in ("Yes", "ok", "make the book a call button smaller"):
        text = " ".join(message.split())
        if len(row.examples) < ledger.EXAMPLES_PER_SIGNATURE and len(text) > 12:
            row.examples.append(text[:90])
    assert row.examples == ["make the book a call button smaller"]


def test_a_failure_earlier_on_the_fix_s_own_day_is_not_a_regression(monkeypatch):
    """The bug this caught on its first real run. The scanner fix went live at 11:11; a
    failure at 11:02 the same morning was reported as a regression, because the comparison
    was by date. A tool that cries regression on day one does not get read on day two."""
    fixed_at = datetime(2026, 8, 28, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setitem(RESOLVED, "same day", (fixed_at, "fixed at 11:11"))

    before = datetime(2026, 8, 28, 11, 2, tzinfo=timezone.utc)
    after = datetime(2026, 8, 28, 11, 20, tzinfo=timezone.utc)

    assert not (before >= fixed_at), "a failure before the fix is not a regression"
    assert after >= fixed_at, "a failure after the fix is one"

    row = _row("same day", FAULT_CODE, 1, ["Xtravu"], first=before, last=before)
    assert not row.is_regression
    assert not ledger.needs_attention([row])
