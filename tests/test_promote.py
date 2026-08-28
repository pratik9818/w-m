"""Growing the eval corpus from failures that really happened.

The corpus used to be ten messages typed out by hand. The bot produces better material
than that every day, and it stores everything needed to replay it: the message, the
conversation around it, and the site's complete files as they were at that moment.

Two things decide whether this is worth having. The cases have to be *answerable* -- an
early run promoted "Yed" and "Yes you do it" as standalone messages, which are not hard
cases but meaningless ones. And the assertions have to be honest: we know what went wrong,
never what should have happened, so a generated case may only forbid the failure it
recorded. Anything stronger is a guess, and a guess in a permanent test is a future
afternoon wasted chasing it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from evals import promote
from worker.learning.outcomes import (
    LABEL_CLARIFY_LOOP,
    LABEL_FAILED,
    LABEL_REASKED,
)
from worker.learning.signatures import FAULT_CODE, FAULT_MODEL

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class FakeOutcome:
    def __init__(self, label, fault=FAULT_MODEL, signature=None, detail=None, at=T0):
        self.label = label
        self.fault = fault
        self.signature = signature
        self.detail = detail or {}
        self.occurred_at = at
        self.edit_log_id = "abcdef12-3456-7890-abcd-ef1234567890"


class FakeEdit:
    def __init__(self, message, applied=False, error=None, operation=None, at=T0):
        self.raw_message = message
        self.applied = applied
        self.error = error
        self.parsed_operation = {"operation": operation} if operation else None
        self.created_at = at


class FakeBusiness:
    name = "NFT Gallery"
    slug = "nft-gallery"


class FakeVersion:
    version_number = 3


def candidate(outcome, edit, context=()):
    return promote.Candidate(outcome, edit, FakeBusiness(), FakeVersion(), list(context))


# ----------------------------------------------- the assertions are honest

def test_a_reasked_edit_only_forbids_the_answer_that_failed():
    """We know this operation did not satisfy the owner. We do not know which one would
    have, and inventing one would bake a guess into a permanent test."""
    edit = FakeEdit("make the top section taller", applied=True, operation="set_style")
    edit.parsed_operation = {"operation": "set_style", "changes": [{"selector": ".hero"}]}
    case = promote.case_from(
        candidate(FakeOutcome(LABEL_REASKED, detail={"gap_seconds": 4260}), edit),
        "regression-nft-gallery-v3",
    )
    assert case["forbid_identical"] == edit.parsed_operation
    assert "expect_operation" not in case, "the right answer is not known, so it is not asserted"
    assert "71 hours" not in case["note"]
    assert "71 minutes later" in case["note"]


def test_a_clarify_loop_forbids_asking_a_third_time():
    case = promote.case_from(
        candidate(FakeOutcome(LABEL_CLARIFY_LOOP), FakeEdit("the top bit")),
        "regression-nft-gallery-v3",
    )
    assert case["forbid_operation"] == ["clarify"]


def test_case_ids_are_stable_so_re_running_updates_instead_of_duplicating():
    outcome = FakeOutcome(LABEL_REASKED)
    first = promote.case_id_for(outcome)
    second = promote.case_id_for(outcome)
    assert first == second == "reasked-abcdef12"


# ----------------------------------------------- the cases are answerable

def test_a_bare_confirmation_carries_the_question_it_was_answering():
    """"Yes you do it" replayed on its own is not a hard case, it is an unanswerable one,
    and a parser that failed it would be right to."""
    history = [
        FakeEdit("there are no images, the site is empty",
                 error="asked: where should the images come from?", at=T0 - timedelta(minutes=5)),
    ]
    context = promote.rebuild_context(history)
    case = promote.case_from(
        candidate(FakeOutcome(LABEL_FAILED, signature="guard: structural"),
                  FakeEdit("Yes you do it"), context),
        "regression-nft-gallery-v1",
    )
    assert case["context"][0]["outcome"]["bot_asked"].startswith("where should the images")


def test_context_reconstructs_each_kind_of_turn():
    """The rendering in session.py keys off these exact shapes; a turn it does not
    recognise silently vanishes from the prompt."""
    turns = promote.rebuild_context([
        FakeEdit("make it green", applied=True, operation="set_style"),
        FakeEdit("and bigger", error="asked: which part?"),
        FakeEdit("the heading", error="rejected: business busy (status=generating)"),
        FakeEdit("add a price table", operation="patch_site"),
    ])
    assert turns[0]["outcome"]["applied"] == "set_style"
    assert turns[1]["outcome"]["bot_asked"] == "which part?"
    assert "rejected" in turns[2]["outcome"]
    # A proposal awaiting a yes is why the next message is often a bare "yes".
    assert "bot_asked" in turns[3]["outcome"]


def test_only_the_recent_turns_are_carried():
    history = [FakeEdit(f"message {i}", applied=True, operation="set_style") for i in range(12)]
    assert len(promote.rebuild_context(history)) == promote.CONTEXT_TURNS


# ----------------------------------------------- merging and housekeeping

def test_a_hand_tightened_case_is_never_overwritten():
    """Once someone has improved a generated assertion, re-running the promoter must not
    throw that work away."""
    existing = [{"id": "reasked-abcdef12", "fixture": "f", "message": "m",
                 "expect_operation": ["set_style"], "occurred_at": "2026-08-26"}]
    fresh = [{"id": "reasked-abcdef12", "fixture": "f", "message": "m",
              "generated": True, "occurred_at": "2026-08-26"}]
    merged = promote.merge(existing, fresh)
    assert len(merged) == 1
    assert merged[0]["expect_operation"] == ["set_style"]


def test_a_generated_case_is_refreshed_in_place():
    existing = [{"id": "reasked-abcdef12", "fixture": "old", "message": "m",
                 "generated": True, "occurred_at": "2026-08-26"}]
    fresh = [{"id": "reasked-abcdef12", "fixture": "new", "message": "m",
              "generated": True, "occurred_at": "2026-08-26"}]
    assert promote.merge(existing, fresh)[0]["fixture"] == "new"


def test_promoting_twice_does_not_duplicate_anything():
    fresh = [{"id": "reasked-abcdef12", "fixture": "f", "message": "m",
              "generated": True, "occurred_at": "2026-08-26"}]
    once = promote.merge([], fresh)
    twice = promote.merge(once, fresh)
    assert len(twice) == 1


def test_orphan_fixtures_are_pruned_but_hand_written_ones_are_left_alone(tmp_path, monkeypatch):
    """A fixture is a whole site, about 40KB. A weekly job that never deletes fills the
    repository with snapshots nothing reads."""
    monkeypatch.setattr(promote, "FIXTURES_DIR", tmp_path)
    (tmp_path / "regression-live-v1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "regression-orphan-v9.json").write_text("{}", encoding="utf-8")
    (tmp_path / "engineer-portfolio.json").write_text("{}", encoding="utf-8")

    removed = promote.prune_orphan_fixtures([{"fixture": "regression-live-v1"}])

    assert removed == ["regression-orphan-v9"]
    assert (tmp_path / "regression-live-v1.json").exists()
    assert (tmp_path / "engineer-portfolio.json").exists(), "hand-written fixtures are not ours"


# ----------------------------------------------- the boundary that matters most

def test_a_code_defect_is_never_promoted_to_a_parser_case():
    """The CSS scanner bug was not a parsing mistake -- the parser was right and the
    executor was broken. A parser eval built from it would pass with the bug still in the
    tree, which is worse than having no eval at all."""
    from worker.learning.outcomes import LABEL_FAILED as FAILED

    code_fault = FakeOutcome(FAILED, fault=FAULT_CODE, signature="set_style: applied but ...")
    model_fault = FakeOutcome(FAILED, fault=FAULT_MODEL, signature="parse: could not read")

    # Mirrors the filter in find_candidates, which is where the decision is made.
    def promotable(outcome):
        return not (outcome.label == FAILED and outcome.fault != FAULT_MODEL)

    assert not promotable(code_fault)
    assert promotable(model_fault)
