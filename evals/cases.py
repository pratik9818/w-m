"""Every case here is a message a real owner actually sent, with what should happen to it.

The prompts in this project are a record of individual bug fixes -- "a real failure: an
owner asked ..." -- and until now nothing checked whether fixing the twelfth one broke the
third. These cases are that check. Add one whenever a message is handled badly; never
delete one because it started failing.

Assertions available per case:

  expect_operation      set of acceptable operation names
  forbid_text           regexes that must NOT appear in the parsed operation
  require_text          regexes that must appear in the parsed operation
  require_beats         {"selector", "property"}: a set_style change on that selector must
                        move the value, and where both sides are plain pixels, upwards
  context               prior turns, exactly as bot_api/services/session.py stores them

Two rules apply to every case automatically (see run_evals.py): a `clarify` question may
never contain developer jargon, and every selector a `set_style` names must actually exist
in the fixture's stylesheet.
"""

FIXTURE = "engineer-portfolio"

# The message this owner sent six times over two days. At the point the fixture was taken,
# .hero is already min-height: 800px and .hero-title is already bold, so re-sending those
# same values is the loop: it patches nothing, fails, and tells them it can't be done.
TALLER_AND_BOLDER = (
    'The section in main page where "software that works for you " present, i am taking '
    "about that section, I want to increase height of that section or part and also bold "
    "the text"
)

CASES = [
    {
        "id": "already-tall-and-bold",
        "fixture": FIXTURE,
        "message": TALLER_AND_BOLDER,
        "expect_operation": {"set_style", "patch_site", "clarify"},
        "forbid_text": [r"800px", r"font-weight\D{0,20}bold"],
        "note": "asking for values already in force is the six-message loop",
    },
    {
        "id": "asking-again-means-more",
        "fixture": FIXTURE,
        "message": TALLER_AND_BOLDER,
        "context": [
            {
                "raw_message": TALLER_AND_BOLDER,
                "outcome": {"rejected": "that changed nothing -- the site already looked "
                                        "exactly like that"},
            }
        ],
        "expect_operation": {"set_style", "patch_site"},
        "require_beats": {"selector": ".hero", "property": "min-height"},
        "note": "a repeat after a no-op means push it further, not ask a question",
    },
    {
        "id": "bigger-must-not-be-smaller",
        "fixture": FIXTURE,
        "message": "make the main heading bigger, it looks tiny",
        "expect_operation": {"set_style", "patch_site"},
        "require_beats": {"selector": ".hero-title", "property": "font-size"},
        "note": "'increase the font size' once produced a flat 32px against a 48px heading",
    },
    {
        "id": "right-element",
        "fixture": FIXTURE,
        "message": "the big heading at the top is still too small, the line under it is fine",
        "expect_operation": {"set_style", "patch_site"},
        "forbid_text": [r"hero-subtitle"],
        "note": "a follow-up about the heading once enlarged the paragraph beneath it",
    },
    {
        "id": "colour-is-a-style-change",
        "fixture": FIXTURE,
        "message": "make the top section dark green instead of blue",
        "expect_operation": {"set_style"},
        "note": "a plain value change should take the deterministic route, not a rewrite",
    },
    {
        "id": "pages-are-not-patchable",
        "fixture": FIXTURE,
        "message": "actually keep only one page, put everything on the home page",
        "expect_operation": {"change_layout"},
        "note": "patching cannot add or delete pages; one such request cost 21,867 tokens",
    },
    {
        "id": "the-photo-is-already-here",
        "fixture": FIXTURE,
        "message": "put that photo in the background of the top section, behind the words",
        "expect_operation": {"patch_site", "set_style"},
        "forbid_text": [r"which photo", r"send (it|the photo) again", r"upload"],
        "note": "an owner was asked which photo they meant after sending it twice",
    },
    {
        "id": "no-invented-testimonials",
        "fixture": FIXTURE,
        "message": "add a testimonials section with some reviews",
        "expect_operation": {"clarify"},
        "note": "attributed third-party claims are never fabricated",
    },
    {
        "id": "both-halves-or-neither",
        "fixture": FIXTURE,
        "message": "remove the FAQ section and make the buttons rounder",
        "expect_operation": {"patch_site", "clarify"},
        "require_text": [r"faq", r"round|radius|corner"],
        "note": "'do the easy half quietly' is the failure this guards",
    },
    {
        "id": "not-every-message-is-an-edit",
        "fixture": FIXTURE,
        "message": "thanks so much, this looks great!",
        "expect_operation": {"not_an_edit"},
        "note": "chit-chat must not start a build",
    },
]
