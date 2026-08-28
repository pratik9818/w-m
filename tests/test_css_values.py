"""The styling digest is what stops the parser asking for a change that is already made,
so the cases below are the ones that actually burned an owner, not a generic sample."""

from worker.codegen.css_values import effective_styles, html_classes, style_digest

# Shaped like a real stored stylesheet: the base.css fallback block first, the generated
# design after it. Anything defined in both must report the generated value -- that is the
# half the browser actually uses.
CSS = """
/* Minimal fallback rules */
.hero, .page-hero { padding: 3rem 0; }
.hero-title { margin: 0; font-size: clamp(1.75rem, 4vw, 3rem); line-height: 1.15; }
.hero-subtitle { margin: 0; font-size: 1.125rem; }

/* ---- generated ---- */
:root {
  --color-primary: #0066ff;
  --space-6x: 3rem;
  --font-weight-bold: 600;
}
.hero,
.page-hero {
  background-color: var(--color-primary);
  padding: var(--space-6x) 0;
}
.hero {
  min-height: 800px;
}
.hero-title {
  font-weight: bold;
}
.hero-bg .hero-title {
  color: #fff;
}
@media (max-width: 600px) {
  .hero-title { font-size: 1.5rem; }
}
"""

FILES = {
    "style.css": CSS,
    "index.html": (
        '<section class="hero"><h1 class="hero-title">Software that works for you</h1>'
        '<p class="hero-subtitle">From concept to launch.</p></section>'
        '<footer class="site-footer"></footer>'
    ),
}


def test_generated_rules_win_over_the_fallback_block():
    styles, _ = effective_styles(CSS)
    # 3rem from the fallback, then var(--space-6x) from the design: same value, but it
    # must arrive resolved, not as a var() the parser cannot compare anything against.
    assert styles["hero"]["padding"] == "3rem 0 (48px 0)"
    assert styles["hero"]["background-color"] == "#0066ff"


def test_the_two_values_that_caused_the_loop_are_reported():
    styles, _ = effective_styles(CSS)
    assert styles["hero"]["min-height"] == "800px"
    assert styles["hero-title"]["font-weight"] == "bold"


def test_a_size_range_is_annotated_with_what_it_renders_as():
    # "increase the font size" became a flat 32px because nobody could see that this
    # clamp reaches 48px on a laptop.
    styles, _ = effective_styles(CSS)
    assert "renders between 28px and 48px" in styles["hero-title"]["font-size"]


def test_media_query_values_are_flagged_but_never_folded_in():
    styles, responsive = effective_styles(CSS)
    assert "hero-title" in responsive
    assert "1.5rem" not in styles["hero-title"]["font-size"]


def test_descendant_selectors_are_not_folded_into_the_class():
    styles, _ = effective_styles(CSS)
    # `.hero-bg .hero-title` only applies inside a background hero; reporting its colour
    # as the heading's colour would send the next edit chasing a value that isn't there.
    assert "color" not in styles["hero-title"]


def test_digest_covers_only_classes_the_pages_actually_use_in_page_order():
    lines = [line for line in style_digest(FILES).splitlines() if line.startswith("  .")]
    assert [line.split(" ->")[0].strip() for line in lines] == [
        ".hero", ".hero-title", ".hero-subtitle",
    ]
    # .page-hero is styled but never used on a page; .site-footer is used but unstyled.
    assert ".page-hero" not in style_digest(FILES)


def test_digest_states_that_headings_are_already_bold():
    # "make the heading bold" was applied four times to an <h1> that was bold to begin
    # with, so every one of those edits was invisible.
    assert "bold" in style_digest(FILES).splitlines()[-1]


def test_html_classes_are_ordered_by_first_appearance():
    assert html_classes(FILES) == ["hero", "hero-title", "hero-subtitle", "site-footer"]


def test_missing_or_empty_stylesheet_is_not_an_error():
    assert style_digest(None) == ""
    assert style_digest({"index.html": "<p>hi</p>"}) == ""
    assert style_digest({"style.css": "", "index.html": "<p>hi</p>"}) == ""


# ------------------------------------------------- the scanner behind every style edit

# Verbatim from a real stored stylesheet (Xtravu). Every generated sheet opens with a
# prose comment the model wrote about the design, and prose has apostrophes in it.
CSS_WITH_APOSTROPHE_IN_COMMENT = """
/* ---- generated ---- */
:root { --accent: #4dff9e; }

/* ============================================================
   Xtravu — shared stylesheet
   Concept: dark control-room / bank of live monitors,
   one signal-green accent showing what's live.
   ============================================================ */

.btn-primary {
  background: var(--accent);
}
.hero-title {
  font-size: 3rem;
}
"""


def test_a_comment_apostrophe_does_not_swallow_the_rest_of_the_stylesheet():
    """The bug that silently disabled style edits on 3 of 15 live sites.

    An apostrophe inside a comment was read as the start of a CSS string. Nothing closed
    it, so on one real sheet the scanner found 3 rules in a file of 115 and every edit to
    anything below that comment was applied, failed verification, and was dropped. The
    owner was told their change could not be made, three times in a row, for a button that
    was perfectly editable.

    `effective_styles` strips comments before scanning so the planner never saw it; only
    the executor, which has to preserve byte offsets, walked into it.
    """
    from worker.codegen.css_values import iter_rule_spans

    preludes = [span.prelude for span in iter_rule_spans(CSS_WITH_APOSTROPHE_IN_COMMENT)]
    assert ".btn-primary" in preludes
    assert ".hero-title" in preludes, "rules after the comment were invisible"


def test_offsets_still_point_into_the_original_text():
    """Comments are skipped, never stripped. Callers splice edits back in by these
    offsets, and stripping would shift every one of them -- turning a silent no-op into a
    corrupted stylesheet, which is worse."""
    from worker.codegen.css_values import iter_rule_spans

    css = CSS_WITH_APOSTROPHE_IN_COMMENT
    for span in iter_rule_spans(css):
        assert css[span.body_start:span.body_end] == span.body


def test_a_real_string_still_ends_at_its_closing_quote():
    """The fix must not go so far that quoted content stops being quoted -- a brace
    inside a string would then be counted and throw the depth tracking off."""
    from worker.codegen.css_values import iter_rule_spans

    css = '.a { content: "} not a rule {"; color: red; }\n.b { color: blue; }\n'
    preludes = [span.prelude for span in iter_rule_spans(css)]
    assert preludes == [".a", ".b"]


def test_an_unterminated_quote_costs_one_line_not_the_whole_file():
    """A malformed sheet should degrade, not disappear. A CSS string cannot contain a raw
    newline, so the line break ends it and the rules below stay reachable."""
    from worker.codegen.css_values import iter_rule_spans

    css = ".a { font-family: 'Unclosed; }\n.b { color: blue; }\n.c { color: red; }\n"
    preludes = [span.prelude for span in iter_rule_spans(css)]
    assert ".b" in preludes and ".c" in preludes
