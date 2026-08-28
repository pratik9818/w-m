"""A list written with commas is still a list.

The failure this exists for read, to the owner, as the bot forgetting. They asked for a
change, the bot proposed it, they replied "Yes also give button thin border" -- and were
answered with "I'm not sure which part of your site you mean". Nothing had been forgotten.

The model had returned `targets` as the single string "index.html, style.css" instead of
an array. Python iterates a string one character at a time, so every filename check ran
against "i", then "n", then "d"; nothing matched a real page; the target list came out
empty; and the request was discarded as unusable.

The lesson in it is not about strings. It is that refusing a message a person plainly
meant, over the difference between a comma and a bracket, is never the right trade -- and
that a rejection an owner cannot act on is indistinguishable from a bot that has lost the
thread.
"""

import pytest

from bot_api.services.edit_ops import coerce_targets, normalize_patch_targets

PAGES = ["index.html", "about.html", "services.html", "contact.html", "style.css"]
LANDING = ["index.html", "style.css"]


# ------------------------------------------------- the exact production failure

def test_the_comma_separated_string_that_broke_a_real_conversation():
    assert coerce_targets("index.html, style.css") == ["index.html", "style.css"]


def test_that_string_now_survives_the_whole_normalising_path():
    """This returned [] before, which the handler read as "no valid targets" and turned
    into a question the owner had already answered."""
    assert normalize_patch_targets("index.html, style.css", available=PAGES) == [
        "index.html", "style.css"
    ]


def test_a_string_target_is_never_read_one_character_at_a_time():
    """The specific mechanism. If this ever regresses, the symptom is not a crash -- it is
    an owner being asked which page they meant, seconds after telling us."""
    coerced = coerce_targets("index.html")
    assert coerced == ["index.html"]
    assert "i" not in coerced and "n" not in coerced


# ------------------------------------------------- every shape a model might send

@pytest.mark.parametrize("value, expected", [
    (["index.html", "style.css"], ["index.html", "style.css"]),   # the documented shape
    ("index.html, style.css", ["index.html", "style.css"]),        # what actually arrived
    ("index.html,style.css", ["index.html", "style.css"]),         # no space
    ("index.html style.css", ["index.html", "style.css"]),         # space only
    (["index.html, style.css"], ["index.html", "style.css"]),      # the same mistake in a list
    ("index.html", ["index.html"]),                                # a lone filename
    ((("index.html",))[0], ["index.html"]),                        # a tuple's contents
    ([], []),
    ("", []),
    (None, []),
    ("   ", []),
])
def test_every_plausible_shape_is_read_rather_than_refused(value, expected):
    assert coerce_targets(value) == expected


def test_a_tuple_or_set_is_accepted_too():
    assert sorted(coerce_targets(("index.html", "style.css"))) == ["index.html", "style.css"]
    assert sorted(coerce_targets({"index.html", "style.css"})) == ["index.html", "style.css"]


# ------------------------------------------------- the behaviour it must not change

def test_a_page_this_site_does_not_have_is_still_redirected():
    """A landing site has only index.html; naming services.html used to fail the build
    with "Nothing to patch". That behaviour predates this fix and must survive it."""
    assert normalize_patch_targets("services.html", available=LANDING) == ["index.html"]


def test_the_stylesheet_alone_is_still_a_valid_target():
    assert normalize_patch_targets("style.css", available=PAGES) == ["style.css"]


def test_something_that_names_no_real_file_still_yields_nothing():
    """Coercion must not become invention. A target list that names nothing on this site
    is still empty, and the handler is still right to ask which page they meant."""
    assert normalize_patch_targets("wibble.txt", available=PAGES) == []


def test_a_widening_helper_can_no_longer_be_handed_a_bare_string():
    """`widen_targets_for_pictures` concatenates a list onto its argument, so a string
    there does not merely fail to match -- it raises TypeError mid-conversation."""
    from bot_api.services.edit_ops import widen_targets_for_pictures

    widened = widen_targets_for_pictures("add a photo at the top", coerce_targets("style.css"))
    assert widened == ["style.css", "index.html"]


def test_the_handler_coerces_before_anything_reads_the_targets():
    """The fix has to sit at the point the model's output is first read. Applied later,
    the widen helpers upstream still see a string."""
    import inspect

    from bot_api.bot.handlers import edit

    source = inspect.getsource(edit.on_free_text_edit) if hasattr(
        edit, "on_free_text_edit") else inspect.getsource(edit)
    assert "widen_targets_for_pictures(instruction, coerce_targets(" in source
