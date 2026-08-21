"""Apply a style change by editing the stylesheet, not by asking a model to rewrite it.

Thirteen of the last seventeen real edit messages on this bot were a style value on
something already on the page -- taller, bolder, a different colour. Every one of them
took the long route: an English instruction written by a parser that could not see the
file, handed to a second model that had to find the element itself and return all 12KB of
the stylesheet with one line different. That route produced, in a single week:

  - a change aimed at `.hero-section` when the class is `.hero`, so nothing happened;
  - a heading set to a flat `32px` when it had been rendering at 48px;
  - three edits applied to the dead `base.css` block, which loses every cascade;
  - two runs that came back byte-identical and were reported to the owner as failures;
  - a 1024-character truncation limit that silently produced half a stylesheet.

None of those failures is possible here. A `set_style` operation names a selector, a
property and a value; this module finds the winning rule and rewrites the few characters
that need to change. No model call, no tokens, no rewrite risk, and the result is
verified against the file afterwards rather than hoped for.
"""
from __future__ import annotations

import logging
import re

from worker.codegen.css_values import (
    GENERATED_MARKER,
    TRACKED_PROPERTIES,
    SIMPLE_SELECTOR_RE,
    effective_styles,
    iter_rule_spans,
    split_generated,
    split_top_level,
)

logger = logging.getLogger(__name__)

STYLESHEET_FILE = "style.css"

# One edit should be a handful of declarations. A request for more than this is a redesign
# wearing a style change's clothes, and belongs on the rebuild path.
MAX_CHANGES = 12
MAX_VALUE_LENGTH = 120

# A property name has to be one the browser actually implements. A misspelling ("colour")
# writes a declaration that parses, applies nothing, and looks exactly like the silent
# no-ops this module exists to eliminate -- so an unknown name is refused outright rather
# than published and discovered later by an owner who can see nothing changed.
ALLOWED_PROPERTIES = frozenset(TRACKED_PROPERTIES) | frozenset({
    "aspect-ratio", "backdrop-filter", "background-position", "background-repeat",
    "background-size", "border-bottom", "border-color", "border-left", "border-right",
    "border-style", "border-top", "border-width", "column-gap", "cursor", "filter",
    "flex", "flex-basis", "flex-grow", "flex-shrink", "flex-wrap", "font-style",
    "grid-template-columns", "grid-template-rows", "justify-items", "list-style",
    "margin-bottom", "margin-left", "margin-right", "margin-top", "max-height",
    "min-width", "object-fit", "order", "outline", "overflow", "overflow-x",
    "overflow-y", "padding-bottom", "padding-left", "padding-right", "padding-top",
    "place-items", "position", "row-gap", "text-decoration", "text-overflow",
    "text-shadow", "transform", "transition", "vertical-align", "visibility",
    "white-space", "word-break", "z-index",
})

_PROPERTY_RE = re.compile(r"^[a-z][a-z-]{1,39}$")
# Anything that could end the declaration early and start writing rules of its own. A
# style value arrives from a model, so it is untrusted input to a file we then publish.
_UNSAFE_VALUE_RE = re.compile(r"[{}<>;@]|/\*|</")
_DECLARATION_TEMPLATE = "  {property}: {value};"


class StyleOpFailed(Exception):
    """The change could not be applied to the stylesheet."""


class StyleAlreadySet(StyleOpFailed):
    """Every value asked for is already in force, so there is nothing to do.

    Not an error in the request and not a fault in the site -- the distinction the owner
    needs, and the one the old pipeline could not make.
    """


def _validate(change: dict) -> tuple[str, str, str]:
    selector = str(change.get("selector") or "").strip()
    prop = str(change.get("property") or "").strip().lower()
    value = str(change.get("value") or "").strip().rstrip(";").strip()

    if not SIMPLE_SELECTOR_RE.match(selector):
        raise StyleOpFailed(
            f"{selector!r} is not a plain class selector like .hero or section.hero"
        )
    if not _PROPERTY_RE.match(prop) or prop not in ALLOWED_PROPERTIES:
        raise StyleOpFailed(f"{prop!r} is not a style property this can set")
    if not value or len(value) > MAX_VALUE_LENGTH or _UNSAFE_VALUE_RE.search(value):
        raise StyleOpFailed(f"{value!r} is not a usable value for {prop}")
    return selector, prop, value


def _class_of(selector: str) -> str:
    match = SIMPLE_SELECTOR_RE.match(selector)
    return match.group(1) if match else ""


def _winning_span(css: str, selector: str):
    """The last top-level rule that styles this selector's class, or None.

    Last rather than first because that is the one the browser applies, and because the
    stored stylesheet deliberately holds two definitions of every contract class: the
    fallback block first, the design's own rules after it.
    """
    target = _class_of(selector)
    winner = None
    for span in iter_rule_spans(css):
        if span.prelude.startswith("@"):
            continue
        for part in split_top_level(span.prelude, ","):
            if _class_of(part) == target:
                winner = span
                break
    return winner


def _declaration_span(body: str, prop: str) -> tuple[int, int] | None:
    """Offsets of an existing declaration's *value* within a rule body."""
    for match in re.finditer(r"(^|;)(\s*)([a-zA-Z-]+)(\s*):([^;]*)", body):
        if match.group(3).strip().lower() == prop:
            value_start = match.start(5)
            return value_start, match.end(5)
    return None


def _indent_of(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line[: len(line) - len(line.lstrip())] or "  "
    return "  "


def _apply_one(css: str, selector: str, prop: str, value: str) -> str:
    """Return `css` with this one declaration in force, editing as little as possible."""
    span = _winning_span(css, selector)

    if span is None:
        # Nothing styles this class in the editable half yet. Appending is safe: a rule at
        # the end wins over everything above it at the same specificity.
        rule = f"\n{selector} {{\n{_DECLARATION_TEMPLATE.format(property=prop, value=value)}\n}}\n"
        return css.rstrip("\n") + "\n" + rule

    existing = _declaration_span(span.body, prop)
    if existing is not None:
        start, end = existing
        return (
            css[: span.body_start + start]
            + f" {value}"
            + css[span.body_start + end:]
        )

    indent = _indent_of(span.body)
    declaration = f"{indent}{prop}: {value};\n"
    body = span.body if span.body.endswith("\n") else span.body + "\n"
    return css[: span.body_start] + body + declaration + css[span.body_end:]


def _current_value(css: str, selector: str, prop: str) -> str | None:
    span = _winning_span(css, selector)
    if span is None:
        return None
    existing = _declaration_span(span.body, prop)
    if existing is None:
        return None
    return span.body[existing[0]:existing[1]].strip()


def describe_current(css: str, changes: list[dict]) -> str:
    """What the stylesheet says today about the properties this change would set.

    Used to tell an owner *why* nothing happened, in terms of the values themselves --
    "the top section's minimum height is already 1200px" -- instead of the old catch-all
    apology that sent them round the same loop.
    """
    styles, _ = effective_styles(css)
    parts = []
    for change in changes:
        try:
            selector, prop, _ = _validate(change)
        except StyleOpFailed:
            continue
        current = styles.get(_class_of(selector), {}).get(prop)
        if current:
            parts.append(f"{selector} {prop} is already {current}")
    return "; ".join(parts)


def apply_style_changes(
    files: dict[str, str], changes: list[dict]
) -> tuple[dict[str, str], list[str]]:
    """Apply `changes` to the stylesheet in `files`.

    Each change is {"selector": ".hero", "property": "min-height", "value": "1200px"},
    optionally carrying "from" (what the planner believed the current value to be, kept
    for the log). Returns (new files, one summary line per change actually made).

    Raises StyleAlreadySet when every value asked for is already in force, and
    StyleOpFailed when a change is malformed or does not survive verification.
    """
    if not changes:
        raise StyleOpFailed("no style changes given")
    if len(changes) > MAX_CHANGES:
        raise StyleOpFailed(f"{len(changes)} changes is more than one edit should make")

    css = files.get(STYLESHEET_FILE)
    if not css:
        raise StyleOpFailed("this site has no stylesheet to change")

    # Only the design's own rules are editable. The fallback block above the marker is
    # overridden by everything below it, so a change written there does nothing at all --
    # a mistake that cost one owner three consecutive edits before anyone noticed.
    prefix, editable = split_generated(css)

    summaries: list[str] = []
    for change in changes:
        selector, prop, value = _validate(change)
        before = _current_value(editable, selector, prop)
        if before is not None and before.strip() == value:
            continue
        editable = _apply_one(editable, selector, prop, value)
        stated = str(change.get("from") or "").strip()
        if stated and before and stated != before:
            logger.info(
                "style.from_mismatch selector=%s property=%s expected=%r actual=%r",
                selector, prop, stated, before,
            )
        summaries.append(f"{selector} {prop}: {before or '(unset)'} -> {value}")

    if not summaries:
        raise StyleAlreadySet(describe_current(css, changes) or "already set that way")

    # Verify against the file rather than trusting the edit. This is the whole advantage
    # of a deterministic executor: the postcondition is checkable, for free, right here.
    for change in changes:
        selector, prop, value = _validate(change)
        if _current_value(editable, selector, prop) != value:
            raise StyleOpFailed(
                f"applied {selector} {prop}: {value} but the stylesheet does not say so"
            )

    patched = dict(files)
    patched[STYLESHEET_FILE] = prefix + editable
    if GENERATED_MARKER in css and GENERATED_MARKER not in patched[STYLESHEET_FILE]:
        raise StyleOpFailed("the stylesheet lost its generated-rules marker")
    return patched, summaries
