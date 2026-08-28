"""A pricing section is a thing the site can have, but only when someone asks for one.

An owner asked for a price section and got prices added to the FAQ. Nothing had gone
wrong in the patch itself -- there was simply no pricing section anywhere in the build
contract, so the model put prices in the only section whose requirements mention the word.

These tests pin both halves of the fix: the vocabulary exists everywhere it needs to, and
it stays off every site that did not ask for it.
"""

from pathlib import Path

import pytest

from bot_api.services.edit_ops import widen_targets_for_pricing
from worker.codegen import builder

PRICING_CLASSES = (
    "pricing-grid", "pricing-card", "pricing-name", "pricing-price",
    "pricing-period", "pricing-features",
)

_CONTRACT = Path(builder.__file__).parent / "prompts" / "_contract.md"
_BASE_CSS = Path(builder.__file__).parent / "base.css"


# --------------------------------------------------- the vocabulary exists end to end

def test_the_pricing_classes_are_part_of_the_contract():
    for name in PRICING_CLASSES:
        assert name in builder.CONTRACT_CLASSES, f"{name} would be read as stylesheet drift"


def test_the_writing_prompt_knows_the_pricing_class_names():
    contract = _CONTRACT.read_text(encoding="utf-8")
    for name in PRICING_CLASSES:
        assert f"`{name}`" in contract


def test_base_css_dresses_a_pricing_section_nobody_styled():
    """No build writes a pricing section, so the site's own stylesheet has never heard of
    these. Without a fallback rule an added section renders as naked text."""
    css = _BASE_CSS.read_text(encoding="utf-8")
    for name in PRICING_CLASSES:
        assert f".{name}" in css, f".{name} would render unstyled after an edit adds it"


# --------------------------------------------------- but it is never built by default

def test_no_page_requirement_asks_for_a_pricing_section():
    """The whole point of the decision: pricing is added on request, never by default, so
    an ordinary build costs exactly what it did before."""
    everything = "\n".join(builder.PAGE_REQUIREMENTS.values()) + builder.LANDING_REQUIREMENTS
    for name in PRICING_CLASSES:
        assert name not in everything, (
            f"{name} in the page requirements would put a pricing section on every site"
        )


def test_the_contract_tells_the_model_not_to_invent_one():
    contract = _CONTRACT.read_text(encoding="utf-8")
    assert "Only build a pricing section when you are actually asked for one" in contract


# --------------------------------------------------- an edit brings the stylesheet along

@pytest.mark.parametrize("instruction", [
    "add a pricing section to the services page",
    "show your prices on the home page",
    "add a section with how much each service costs",
])
def test_a_pricing_edit_also_patches_the_stylesheet(instruction):
    assert widen_targets_for_pricing(instruction, ["services.html"]) == [
        "services.html", "style.css",
    ]


def test_an_unrelated_edit_is_left_alone():
    """Widening every edit would put the stylesheet -- the largest file in the build -- in
    the patch set for changes that never touch it."""
    assert widen_targets_for_pricing(
        "make the heading bigger", ["index.html"]
    ) == ["index.html"]


def test_a_stylesheet_only_pricing_edit_is_not_widened_twice():
    assert widen_targets_for_pricing("change the price colour", ["style.css"]) == ["style.css"]


def test_nothing_is_added_when_there_is_no_page_to_add_it_to():
    assert widen_targets_for_pricing("add pricing", []) == []
