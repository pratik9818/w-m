"""Parsing a generation response — the shapes that have actually come back.

A first build is two or three concurrent calls, so one badly-shaped response discards the
others too. These cases decide which shapes are worth rescuing and which must still fail.
"""

import pytest

from worker.codegen.builder import (
    GenerationFailed,
    _files_from_response,
    _format_reminder,
)

# What killed the CryptoPulse Token build: a complete landing page that opened with
# <title> instead of its ===FILE:=== marker.
UNMARKED_PAGE = (
    "<title>CryptoPulse Token</title>\n"
    '<main class="page"><h1>CryptoPulse Token</h1><p>Clear, reliable help.</p></main>'
)

MARKED_PAGE = f"===FILE: index.html===\n{UNMARKED_PAGE}\n===END==="


def test_a_marked_response_is_parsed_normally():
    files = _files_from_response(MARKED_PAGE, ("index.html",), fragments=True)
    assert "CryptoPulse" in files["index.html"]


def test_one_expected_file_without_markers_is_taken_as_that_file():
    files = _files_from_response(UNMARKED_PAGE, ("index.html",), fragments=True)
    assert files["index.html"].endswith("</main>")


def test_a_truncated_page_is_still_refused():
    # Same rescue path, but the page never closes -- exactly the silent half-a-page the
    # free tier used to publish.
    with pytest.raises(GenerationFailed, match="truncated"):
        _files_from_response("<h1>CryptoPulse</h1><p>Clear", ("index.html",), fragments=True)


def test_several_expected_files_are_never_guessed_at():
    with pytest.raises(GenerationFailed, match="missing required file"):
        _files_from_response(UNMARKED_PAGE, ("index.html", "about.html"), fragments=True)


def test_an_empty_response_is_refused():
    with pytest.raises(GenerationFailed, match="missing required file"):
        _files_from_response("   ", ("index.html",), fragments=True)


def test_the_reminder_names_every_file_and_the_closing_element():
    reminder = _format_reminder(("index.html", "about.html"), "</main>")
    assert "===FILE: index.html===" in reminder
    assert "===FILE: about.html===" in reminder
    assert "</main>" in reminder
