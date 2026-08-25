"""A broken script must be caught cheaply, and a browser-only defect must be repairable.

Both halves of the first failed JavaScript build. The page's script had a stray bracket,
which cost a full sandbox run to discover -- and then could not be repaired at all,
because repair_files re-derived the problem list from the markup, found the markup
perfectly fine, and reported nothing to fix while the build died.
"""

import pytest

from worker.codegen import builder, validate
from worker.codegen.js_check import js_problems, script_problems

PAGE = "<title>T</title><h1>T</h1><main>%s</main>"


# ------------------------------------------------------------------ the syntax check

def test_the_stray_bracket_that_failed_the_first_build_is_caught():
    assert script_problems("if (a === b)) { go(); }") == ["unexpected ')'"]


@pytest.mark.parametrize(
    "code",
    [
        "document.addEventListener('DOMContentLoaded', function () { init(); });",
        "const el = document.querySelector('.hero'); if (!el) { return; }",
        "el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300 });",
        "// a comment with an unclosed ( paren\nconst a = 1;",
        "/* block ) ] } */ const a = [1, 2].map(function (n) { return n * 2; });",
        "const s = 'a string with ) and ] in it';",
        "const t = `a template ) with ${value} inside`;",
        "const u = \"don't trip on the apostrophe\";",
        "const re = /[)(]/g; const x = (1 + 2);",  # regex literal -- must not be judged
        "const half = total / 2;",
    ],
)
def test_valid_scripts_are_left_alone(code):
    assert script_problems(code) == []


@pytest.mark.parametrize(
    "code,expected",
    [
        ("function go( { return 1; }", "'(' is never closed"),
        ("const a = [1, 2;", "'[' is never closed"),
        ("const s = 'never closed;\nconst b = 2;", "unterminated '...' string"),
        ("/* never closed\nconst a = 1;", "unterminated /* comment"),
    ],
)
def test_definite_breakage_is_reported(code, expected):
    assert script_problems(code) == [expected]


def test_a_script_with_a_src_is_somebody_elses_problem():
    # Its contents are not ours to parse, and an empty body is not a syntax error.
    assert js_problems('<script src="https://cdn.example/x.js"></script>') == []


def test_the_check_runs_over_the_whole_page():
    check = next(
        c
        for c in validate.validate_files(
            {"index.html": PAGE % "<script>if (a)) {}</script>", "style.css": ""}
        )
        if c["name"] == "javascript_parses"
    )
    assert not check["passed"]
    # Prefixed with the filename, so a repair can be aimed at it.
    assert check["detail"] == ["index.html: unexpected ')'"]


def test_a_page_with_sound_script_passes():
    check = next(
        c
        for c in validate.validate_files(
            {"index.html": PAGE % "<script>const a = (1 + 2);</script>", "style.css": ""}
        )
        if c["name"] == "javascript_parses"
    )
    assert check["passed"]


# ------------------------------------------------------------------ the repair path

# Long enough to clear content_present's floor, so the only thing wrong with this page is
# whatever a test deliberately puts wrong.
BODY = " ".join(["Real body copy about a real business."] * 30)
SOUND_PAGE = {
    "index.html": (
        f"<title>T</title><h1>T</h1><main><p>{BODY}</p>"
        "<script>const a = (1 + 2);</script></main>"
    ),
    "style.css": ".hero { color: red; }",
}


@pytest.mark.asyncio
async def test_a_browser_only_failure_is_actually_sent_for_repair(monkeypatch):
    """The bug: console errors were silently dropped instead of repaired.

    The markup here is flawless, so re-deriving the problem list finds nothing. The only
    evidence the page is broken is the check the browser handed us, which repair_files
    used to ignore entirely.
    """
    called = {}

    async def fake_generate(prompt, expected, **kwargs):
        called["prompt"] = prompt
        return {expected[0]: SOUND_PAGE[expected[0]]}, {
            "model": "stub", "input_tokens": 10, "output_tokens": 10
        }

    monkeypatch.setattr(builder, "_generate", fake_generate)

    console_error = {
        "name": "no_console_errors",
        "passed": False,
        "detail": ["index.html: Unexpected token ')'"],
    }
    _files, usage, _remaining = await builder.repair_files(dict(SOUND_PAGE), [console_error])

    assert usage is not None, "repair never ran: the browser's verdict was thrown away"
    assert "Unexpected token" in called["prompt"]


@pytest.mark.asyncio
async def test_a_clean_build_still_costs_no_repair_call(monkeypatch):
    async def explode(*args, **kwargs):
        raise AssertionError("repair should not have been called")

    monkeypatch.setattr(builder, "_generate", explode)
    files, usage, remaining = await builder.repair_files(dict(SOUND_PAGE), [])
    assert usage is None and remaining == []
    assert files == SOUND_PAGE
