"""Teaching the parser what already worked on this particular site.

Every site's class names are invented fresh by the model that wrote its stylesheet, so
"the book a call button" is `.btn-primary` here and `.cta-button` next door. The parser
re-derives that on every message, and when it guesses wrong the edit runs cleanly, changes
the wrong thing, and the owner asks again -- eleven times in the last ninety days.

The owner's own history answers that question already. These tests are about the two ways
that could go wrong: teaching an answer that did not actually work, and letting the cost
of teaching grow with the age of the site.
"""

from worker.learning.lessons import (
    MAX_LESSONS,
    MIN_RELEVANCE,
    Lesson,
    _describe,
    candidate_query,
    estimate_tokens,
    rank,
    relevance,
    render_lessons,
)
from worker.learning.outcomes import LABEL_APPLIED, LABEL_REASKED


# ------------------------------------------------- finding the right past edit

def test_the_same_request_in_different_words_is_recognised():
    """The real pair this exists for: the owner asked about the same button twice, weeks
    apart, and the second time the parser should not have to guess again."""
    score = relevance(
        "reduce the size of book a call button",
        "Reduce the size of book a call demo in desktop screen",
    )
    assert score >= MIN_RELEVANCE


def test_unrelated_requests_do_not_match():
    """A wrong lesson is worse than no lesson: it points the parser confidently at the
    wrong part of the page."""
    score = relevance("change my opening hours", "make the gallery photos bigger")
    assert score < MIN_RELEVANCE


def test_matching_only_on_filler_words_is_not_a_match():
    """"make the" and "i want to" appear in nearly every message ever sent. A pair sharing
    only those has told us nothing about which part of the site is meant."""
    assert relevance("please make the thing", "please make the other") < MIN_RELEVANCE


def test_an_empty_message_scores_nothing_rather_than_raising():
    assert relevance("", "make the heading bigger") == 0.0


# ------------------------------------------------- what gets remembered

def test_a_style_change_records_which_class_the_words_meant():
    """The whole point. The selector is the fact the parser keeps rediscovering."""
    name, detail = _describe({
        "operation": "set_style",
        "changes": [
            {"selector": ".btn-primary", "property": "padding", "value": "0.5rem 1rem"},
            {"selector": ".btn-primary", "property": "font-size", "value": "0.85rem"},
        ],
    })
    assert name == "set_style"
    assert ".btn-primary padding" in detail
    assert "0.5rem" not in detail, "the value was right for that message, not for the next one"


def test_a_patch_records_which_pages_it_touched():
    name, detail = _describe({"operation": "patch_site", "targets": ["index.html", "style.css"]})
    assert name == "patch_site"
    assert "index.html" in detail


def test_an_unrecognised_operation_still_yields_its_name():
    assert _describe({"operation": "change_layout"})[0] == "change_layout"
    assert _describe({})[0] == ""


# ------------------------------------------------- only teach what worked

def test_only_applied_edits_are_eligible():
    """An edit the owner had to ask for twice carries the label `reasked` precisely so it
    can be excluded here. Teaching an answer that did not satisfy someone is strictly
    worse than teaching nothing, so this checks the query itself rather than trusting it.
    """
    import uuid

    sql = str(candidate_query(uuid.uuid4()).compile(
        compile_kwargs={"literal_binds": True}
    ))
    assert f"= '{LABEL_APPLIED}'" in sql
    assert LABEL_REASKED not in sql, "reasked edits must never be taught"


def test_ranking_puts_the_closest_match_first_and_caps_the_list():
    rows = [
        ("make the gallery photos bigger", {"operation": "set_style",
                                            "changes": [{"selector": ".gallery-img",
                                                         "property": "width"}]}),
        ("reduce the size of book a call demo", {"operation": "set_style",
                                                 "changes": [{"selector": ".btn-primary",
                                                              "property": "padding"}]}),
    ]
    ranked = rank(rows, "make the book a call button smaller")
    assert ranked, "the matching past edit was not found"
    assert ".btn-primary" in ranked[0].detail
    assert len(ranked) <= MAX_LESSONS


def test_the_same_lesson_is_not_repeated():
    """One site asked for the same change on four pages. Four identical lines teach no
    more than one and crowd out everything else."""
    row = ("make the heading bigger", {"operation": "set_style",
                                       "changes": [{"selector": ".hero-title",
                                                    "property": "font-size"}]})
    assert len(rank([row] * 4, "make the heading bigger")) == 1


def test_rows_that_are_not_operations_are_skipped():
    assert rank([("something", None), ("other", "not a dict")], "something") == []


# ------------------------------------------------- cost stays flat

def test_the_number_of_examples_is_capped():
    """A site with two years of history must not cost more per message than a new one."""
    assert MAX_LESSONS <= 8


def test_the_injection_stays_small():
    """Measured against the real worst case seen on live data (~101 tokens). The budget
    this feature was justified on was 500; anything approaching that needs re-costing."""
    lessons = [
        Lesson(
            message="Reduce the size of book a call demo in desktop screen",
            operation="set_style",
            detail=".btn-primary padding, .btn-primary font-size",
            score=0.5,
        )
    ] * MAX_LESSONS
    assert estimate_tokens(render_lessons(lessons)) < 500


def test_nothing_relevant_means_nothing_is_added():
    """The common case on a new site, and it must cost exactly zero extra tokens."""
    assert render_lessons([]) == ""


# ------------------------------------------------- how it is worded

def test_lessons_are_offered_as_evidence_not_as_a_rule():
    """An owner is entitled to mean something new by the same words. A prompt that said
    "always use .btn-primary for the button" would stop the parser noticing that."""
    rendered = render_lessons([
        Lesson("make the button smaller", "set_style", ".btn-primary padding", 0.6)
    ])
    assert "not a rule" in rendered
    assert "ignore them" in rendered
    assert "always" not in rendered.lower()


def test_the_owner_s_own_words_are_what_is_shown():
    rendered = render_lessons([
        Lesson("make the book a call button smaller", "set_style", ".btn-primary padding", 0.6)
    ])
    assert "book a call button" in rendered
    assert ".btn-primary" in rendered


def test_the_parser_still_runs_with_no_lessons_at_all():
    """The eval harness calls the parser with no database, so lessons must be optional
    and default to costing nothing."""
    import inspect

    from bot_api.services.nl_edit import parse_edit_message

    signature = inspect.signature(parse_edit_message)
    assert signature.parameters["lessons"].default == ""
