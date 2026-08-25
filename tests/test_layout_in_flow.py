"""A layout container may never be positioned out of the document flow.

The failure this comes from: a generated stylesheet carried

    .hero-bg, .page-hero.hero-bg { position: absolute; inset: 0; }

`.hero-bg` is the hero <section> itself, so the hero stopped reserving height and the next
section rendered 681px underneath it. Measured in a real browser, before and after. The
owner reported the overlap six times; every attempted fix added margin or padding, which
cannot move an absolutely positioned element, so all six failed.
"""

import pytest

from worker.codegen.builder import _keep_layout_in_flow

BROKEN = """.hero-bg,
.page-hero.hero-bg {
  position: absolute;
  inset: 0;
  background-size: cover;
  padding: 8rem;
}"""


def test_the_rule_that_broke_the_real_site_is_neutralised():
    out, fixed = _keep_layout_in_flow(BROKEN)
    assert "position: absolute" not in out
    assert fixed == [".hero-bg, .page-hero.hero-bg"]


def test_only_the_position_declaration_is_removed():
    out, _ = _keep_layout_in_flow(BROKEN)
    # The rest of the rule is the design the model meant; it stays.
    assert "background-size: cover" in out
    assert "padding: 8rem" in out
    assert "inset: 0" in out


@pytest.mark.parametrize(
    "css",
    [
        # Overlays are built with positioned pseudo-elements -- base.css does this itself.
        '.hero-bg::before { content: ""; position: absolute; inset: 0; }',
        ".nav-link.is-current::after { position: absolute; bottom: -2px; }",
        ".section-alt::after { position: absolute; height: 2px; }",
        # A decorative class of the model's own invention is not a contract container.
        ".floating-badge { position: absolute; top: 1rem; }",
        # Relative positioning keeps the element in flow and is how overlays are anchored.
        ".hero-bg { position: relative; }",
        # Sticky headers are a real design choice and still reserve their space.
        ".site-header { position: sticky; top: 0; }",
    ],
)
def test_legitimate_positioning_is_left_alone(css):
    out, fixed = _keep_layout_in_flow(css)
    assert out == css
    assert fixed == []


@pytest.mark.parametrize(
    "selector",
    [".hero", ".page-hero", ".section", ".section-alt", ".cta-band", ".card", ".container"],
)
def test_every_layout_container_is_covered(selector):
    _out, fixed = _keep_layout_in_flow(selector + " { position: fixed; top: 0; }")
    assert fixed, f"{selector} may be taken out of flow"


def test_a_container_nested_in_a_descendant_selector_is_still_protected():
    out, fixed = _keep_layout_in_flow(".hero .card { position: absolute; }")
    assert fixed == [".hero .card"]
    assert "position: absolute" not in out


def test_styling_a_child_of_a_container_is_not_the_container():
    # The subject here is .badge, which is not a layout container.
    css = ".hero .badge { position: absolute; top: 0; }"
    out, fixed = _keep_layout_in_flow(css)
    assert (out, fixed) == (css, [])


def test_rules_inside_a_media_query_are_cleaned_too():
    out, fixed = _keep_layout_in_flow(
        "@media (max-width: 600px) { .hero-bg { position: absolute; inset: 0; } }"
    )
    assert fixed == [".hero-bg"]
    assert "position: absolute" not in out
    assert "@media (max-width: 600px)" in out


def test_a_clean_stylesheet_is_returned_untouched():
    css = ".hero { padding: 4rem 0; }\n.card { border-radius: 8px; }"
    assert _keep_layout_in_flow(css) == (css, [])
