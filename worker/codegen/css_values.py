"""What a site's styling currently *says*, resolved into plain values.

The edit parser could see the shape of the pages (`outline.py`) but not one style value,
so it had no way to tell a change it was about to ask for from one already made. That put
a real owner in a two-day loop: they asked six times to make the top section taller and
its heading bold. After the first attempt the section was already `min-height: 800px` and
the heading -- an `<h1>` -- had been bold before anyone touched it. Every later attempt
re-sent "set .hero min-height to 800px and .hero-title font-weight to bold", patched
nothing, and came back to the owner as "I couldn't work out how to make that change".

The same blindness made "increase the font size" *shrink* the heading: the value was
`clamp(1.75rem, 4vw, 3rem)` -- up to 48px -- and a flat `32px` looked like an increase to
something that could not see the original.

Deterministic string work over the stored stylesheet, no API call and no browser: parse
the rules, resolve `var()` against `:root`, convert rem to px, and report the handful of
properties owners actually talk about.
"""
from __future__ import annotations

import re
from typing import Iterator, NamedTuple

# Ordered by how often an owner asks about them -- the digest prints them in this order,
# so the first thing on every line is the thing most likely to be under discussion.
TRACKED_PROPERTIES = (
    "font-size", "font-weight", "color", "background-color", "background-image",
    "background", "text-align", "text-transform", "letter-spacing", "line-height",
    "min-height", "height", "max-width", "width", "padding", "margin", "gap",
    "border", "border-radius", "box-shadow", "opacity", "display", "flex-direction",
    "justify-content", "align-items", "font-family",
)

# Enough to cover a page's visible vocabulary without pasting the whole stylesheet into
# the prompt. Ordered by first appearance in the markup, so the hero comes first and the
# footer last -- if anything is cut, it is the least-discussed end of the page.
MAX_CLASSES = 45

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_VAR_RE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,([^()]*))?\)")
# `.hero` and `section.hero` fold into the same entry; anything with a descendant,
# pseudo-class or combinator does not, because folding it would misreport the cascade.
_SIMPLE_SELECTOR_RE = re.compile(r"^(?:[a-zA-Z][\w-]*)?\.([\w-]+)$")
_REM_RE = re.compile(r"(?<![\w.-])(\d*\.?\d+)rem\b")
_CLAMP_RE = re.compile(r"clamp\(([^()]*)\)")
_CLASS_ATTR_RE = re.compile(r'class\s*=\s*"([^"]*)"')

_REM_PX = 16.0

# The line build_site() writes between the base.css fallback block and the model's own
# rules. Everything before it loses every cascade it takes part in, so an edit made above
# it is invisible -- three consecutive real edits were lost that way.
GENERATED_MARKER = "/* ---- generated ---- */"

# Public aliases for the applier in style_ops.py, which needs the same notion of "a
# selector we can safely fold or edit" that the digest uses.
SIMPLE_SELECTOR_RE = _SIMPLE_SELECTOR_RE


def split_generated(css: str) -> tuple[str, str]:
    """Split a stored stylesheet into (fallback prefix, the design's own editable rules).

    Returns ("", css) for a stylesheet written before the marker existed, in which case
    the whole file is editable.
    """
    index = css.find(GENERATED_MARKER)
    if index == -1:
        return "", css
    cut = index + len(GENERATED_MARKER)
    return css[:cut], css[cut:]


def split_top_level(text: str, separator: str) -> list[str]:
    return _split_top_level(text, separator)


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on `separator`, ignoring any inside parentheses.

    `font-family: system-ui, sans-serif` and `clamp(1rem, 4vw, 3rem)` both contain commas
    that are not separators.
    """
    parts: list[str] = []
    depth = 0
    buffer: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == separator and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return [part for part in (raw.strip() for raw in parts) if part]


class RuleSpan(NamedTuple):
    """One `prelude { body }` rule, with the offsets needed to edit it in place.

    Offsets are what make a deterministic style edit possible: the applier rewrites the
    handful of characters between `body_start` and `body_end` and leaves every other byte
    of the stylesheet untouched, which is a guarantee no model rewriting the whole file
    can offer.
    """

    prelude: str
    body: str
    body_start: int  # index just after the opening brace
    body_end: int  # index of the closing brace


def iter_rule_spans(css: str) -> Iterator[RuleSpan]:
    """Yield every top-level `prelude { body }` in source order, with offsets."""
    depth = 0
    start = 0
    opened_at = -1
    quote = ""
    for index, char in enumerate(css):
        if quote:
            if char == quote and css[index - 1] != "\\":
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
            if depth == 1:
                opened_at = index
        elif char == "}":
            if depth == 1:
                yield RuleSpan(
                    prelude=css[start:opened_at].strip(),
                    body=css[opened_at + 1:index],
                    body_start=opened_at + 1,
                    body_end=index,
                )
                start = index + 1
            depth = max(depth - 1, 0)


def _scan_blocks(css: str) -> Iterator[tuple[str, str]]:
    for span in iter_rule_spans(css):
        yield span.prelude, span.body


def _iter_rules(css: str, *, in_media: bool = False) -> Iterator[tuple[str, str, bool]]:
    """Every style rule in source order, descending into `@media` and friends.

    At-rules whose body is not made of ordinary rules (`@keyframes`, `@font-face`) are
    skipped rather than misread as selectors.
    """
    for prelude, body in _scan_blocks(css):
        if prelude.startswith("@"):
            at_rule = prelude.split(None, 1)[0].lower()
            if at_rule in ("@media", "@supports", "@layer", "@container"):
                yield from _iter_rules(body, in_media=True)
            continue
        yield prelude, body, in_media


def _declarations(body: str) -> list[tuple[str, str]]:
    declarations: list[tuple[str, str]] = []
    for part in _split_top_level(body, ";"):
        name, separator, value = part.partition(":")
        if separator and value.strip():
            declarations.append((name.strip().lower(), value.strip()))
    return declarations


def _root_variables(css: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for prelude, body, _ in _iter_rules(css):
        if ":root" not in prelude and prelude != "html":
            continue
        for name, value in _declarations(body):
            if name.startswith("--"):
                variables[name] = value
    return variables


def _resolve_vars(value: str, variables: dict[str, str], _depth: int = 0) -> str:
    """Replace `var(--x)` with what it actually is.

    `min-height: var(--space-6x)` tells the parser nothing it can compare against; `48px`
    tells it everything. Variables that reference other variables resolve too, with a
    depth cap so a circular definition cannot hang the prompt build.
    """
    if "var(" not in value or _depth > 3:
        return value

    def substitute(match: re.Match) -> str:
        name, fallback = match.group(1), (match.group(2) or "").strip()
        return variables.get(name) or fallback or match.group(0)

    resolved = _VAR_RE.sub(substitute, value)
    if resolved == value:
        return value
    return _resolve_vars(resolved, variables, _depth + 1)


def _format_px(pixels: float) -> str:
    return f"{pixels:.0f}px" if float(pixels).is_integer() else f"{pixels:g}px"


def _to_px(token: str) -> str | None:
    token = token.strip()
    if match := re.fullmatch(r"(\d*\.?\d+)rem", token):
        return _format_px(float(match.group(1)) * _REM_PX)
    if re.fullmatch(r"\d*\.?\d+px", token):
        return token
    return None


def _annotate(value: str) -> str:
    """Add the rendered size next to anything an owner would think of as a size.

    A model choosing "bigger" needs a number to beat. `clamp(1.75rem, 4vw, 3rem)` is not
    one; "renders between 28px and 48px" is, and it is the difference between the heading
    growing and the heading being set to 32px because 32 looked like a big number.
    """
    if clamp := _CLAMP_RE.search(value):
        arguments = _split_top_level(clamp.group(1), ",")
        if len(arguments) == 3:
            smallest, largest = _to_px(arguments[0]), _to_px(arguments[2])
            if smallest and largest:
                return f"{value} — renders between {smallest} and {largest}"
    if _REM_RE.search(value):
        converted = _REM_RE.sub(lambda m: _format_px(float(m.group(1)) * _REM_PX), value)
        if converted != value:
            return f"{value} ({converted})"
    return value


def effective_styles(css: str) -> tuple[dict[str, dict[str, str]], set[str]]:
    """Fold the stylesheet into {class: {property: value}} as the browser would see it.

    Later rules overwrite earlier ones, which is what makes this honest about the two
    halves of a stored stylesheet: the fallback block at the top is what `base.css`
    contributed, the generated rules below it win, and only the winner is reported.

    Returns (styles, classes_with_narrow_screen_overrides). Media-query rules are counted
    but never folded in -- reporting a phone-only font size as *the* font size would send
    the next edit chasing a value the owner cannot see on their laptop.
    """
    css = _COMMENT_RE.sub("", css)
    variables = _root_variables(css)

    styles: dict[str, dict[str, str]] = {}
    responsive: set[str] = set()
    for prelude, body, in_media in _iter_rules(css):
        classes = [
            match.group(1)
            for selector in _split_top_level(prelude, ",")
            if (match := _SIMPLE_SELECTOR_RE.match(selector))
        ]
        if not classes:
            continue
        if in_media:
            responsive.update(classes)
            continue
        for name, value in _declarations(body):
            if name not in TRACKED_PROPERTIES:
                continue
            rendered = _annotate(_resolve_vars(value, variables))
            for class_name in classes:
                styles.setdefault(class_name, {})[name] = rendered
    return styles, responsive


def html_classes(files: dict[str, str]) -> list[str]:
    """Every class used in the markup, in the order a visitor meets it down the page."""
    order: list[str] = []
    pages = sorted(
        (name for name in files if name.endswith(".html")),
        key=lambda name: (name != "index.html", name),
    )
    for page in pages:
        for attribute in _CLASS_ATTR_RE.findall(files[page]):
            for class_name in attribute.split():
                if class_name not in order:
                    order.append(class_name)
    return order


def style_digest(files: dict[str, str] | None) -> str:
    """The styling map handed to the edit parser, or "" when there is nothing to show."""
    if not files:
        return ""
    css = files.get("style.css")
    if not css:
        return ""

    styles, responsive = effective_styles(css)
    used = [name for name in html_classes(files) if styles.get(name)]
    if not used:
        return ""

    shown = used[:MAX_CLASSES]
    lines = [
        "What the styling says RIGHT NOW (already resolved -- these are the values in "
        "force on the live site):",
    ]
    for class_name in shown:
        declarations = styles[class_name]
        rendered = "; ".join(
            f"{prop}: {declarations[prop]}"
            for prop in TRACKED_PROPERTIES
            if prop in declarations
        )
        lines.append(f"  .{class_name} -> {rendered}")

    narrow = [f".{name}" for name in shown if name in responsive]
    if narrow:
        lines.append(
            f"  (on narrow phone screens {', '.join(narrow[:6])} switch to their own "
            "sizes; the values above are what a laptop shows.)"
        )
    lines.append(
        "  (h1/h2/h3 headings are bold and larger than body text by default, whether or "
        "not a rule says so.)"
    )
    return "\n".join(lines)
