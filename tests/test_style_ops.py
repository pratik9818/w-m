"""Every case here is a failure the LLM patch route actually produced on a live site."""

import pytest

from worker.codegen.css_values import effective_styles
from worker.codegen.style_ops import (
    StyleAlreadySet,
    StyleOpFailed,
    apply_style_changes,
)

CSS = """/* fallback */
.hero, .page-hero { padding: 3rem 0; }
.hero-title { margin: 0; font-size: clamp(1.75rem, 4vw, 3rem); line-height: 1.15; }

/* ---- generated ---- */
:root {
  --color-primary: #0066ff;
}
.hero {
  min-height: 800px;
  background-color: var(--color-primary);
}
.hero-title {
  font-weight: bold;
}
@media (max-width: 600px) {
  .hero { min-height: 400px; }
}
"""

FILES = {"style.css": CSS, "index.html": '<section class="hero"></section>'}


def _css(files):
    return files["style.css"]


def test_changes_the_declaration_and_nothing_else():
    patched, summaries = apply_style_changes(
        FILES, [{"selector": ".hero", "property": "min-height", "from": "800px",
                 "value": "1200px"}]
    )
    assert summaries == [".hero min-height: 800px -> 1200px"]
    before_lines = CSS.splitlines()
    after_lines = _css(patched).splitlines()
    assert len(before_lines) == len(after_lines)
    assert [
        (a, b) for a, b in zip(before_lines, after_lines) if a != b
    ] == [("  min-height: 800px;", "  min-height: 1200px;")]


def test_a_property_the_rule_does_not_have_yet_is_added_to_it():
    patched, _ = apply_style_changes(
        FILES, [{"selector": ".hero-title", "property": "font-size", "value": "56px"}]
    )
    styles, _ = effective_styles(_css(patched))
    assert styles["hero-title"]["font-size"] == "56px"
    assert styles["hero-title"]["font-weight"] == "bold"  # untouched


def test_a_class_with_no_rule_of_its_own_gets_one_appended():
    patched, _ = apply_style_changes(
        FILES, [{"selector": ".hero-subtitle", "property": "color", "value": "#fff"}]
    )
    styles, _ = effective_styles(_css(patched))
    assert styles["hero-subtitle"]["color"] == "#fff"


def test_the_dead_fallback_block_is_never_edited():
    # Three real edits were applied above the marker, where the design's own rules
    # override them, so the page looked identical every time.
    patched, _ = apply_style_changes(
        FILES, [{"selector": ".hero", "property": "padding", "value": "6rem 0"}]
    )
    fallback, generated = _css(patched).split("/* ---- generated ---- */")
    assert "padding: 3rem 0" in fallback
    assert "padding: 6rem 0" in generated


def test_media_query_rules_are_left_alone():
    patched, _ = apply_style_changes(
        FILES, [{"selector": ".hero", "property": "min-height", "value": "1200px"}]
    )
    assert "min-height: 400px" in _css(patched)  # the phone rule survives


def test_a_value_already_in_force_is_not_an_error_but_a_distinct_signal():
    with pytest.raises(StyleAlreadySet) as excinfo:
        apply_style_changes(
            FILES, [{"selector": ".hero", "property": "min-height", "value": "800px"}]
        )
    assert "already 800px" in str(excinfo.value)


def test_a_mixed_request_applies_the_part_that_is_not_already_set():
    patched, summaries = apply_style_changes(
        FILES,
        [
            {"selector": ".hero", "property": "min-height", "value": "800px"},
            {"selector": ".hero-title", "property": "font-weight", "value": "900"},
        ],
    )
    assert summaries == [".hero-title font-weight: bold -> 900"]
    styles, _ = effective_styles(_css(patched))
    assert styles["hero-title"]["font-weight"] == "900"


@pytest.mark.parametrize(
    "change",
    [
        {"selector": ".hero:hover", "property": "color", "value": "red"},
        {"selector": ".hero .title", "property": "color", "value": "red"},
        {"selector": ".hero", "property": "colour", "value": "red"},
        {"selector": ".hero", "property": "color", "value": "red; } body { display: none"},
        {"selector": ".hero", "property": "color", "value": "</style><script>x()</script>"},
        {"selector": ".hero", "property": "color", "value": ""},
    ],
)
def test_unsafe_or_unsupported_changes_are_refused(change):
    with pytest.raises(StyleOpFailed):
        apply_style_changes(FILES, [change])


def test_a_site_without_a_stylesheet_is_refused_cleanly():
    with pytest.raises(StyleOpFailed):
        apply_style_changes({"index.html": "<p>hi</p>"}, [
            {"selector": ".hero", "property": "color", "value": "red"}
        ])


def test_a_stylesheet_predating_the_marker_is_still_editable():
    old_style = {"style.css": ".hero { min-height: 100px; }\n"}
    patched, _ = apply_style_changes(
        old_style, [{"selector": ".hero", "property": "min-height", "value": "900px"}]
    )
    assert "min-height: 900px" in patched["style.css"]
