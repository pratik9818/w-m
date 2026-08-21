"""A compact, readable map of what is actually on a site's pages.

The edit parser decides *what* to change and *which files* to touch, but until now it saw
only the business's data and a list of filenames -- never the files themselves. Every class
name, every "is there already a picture there", every "which page is this on" was therefore
a guess, and the guesses were wrong in five separate ways in a single evening:

  - it sent a photo change to `style.css` alone, where no photo lives, so nothing happened;
  - it asked "which photo would you like?" about pictures already on the page;
  - it wrote `.hero-section`, a class that does not exist -- the real one is `.hero` -- so
    the same edit failed twice and the owner was told it could not be done;
  - it added a second copy of a picture that was already in the hero;
  - it reached for a background image when no such vocabulary existed.

Each was fixed individually. This exists to remove the cause rather than the symptoms: give
the parser a view of the real page before it decides anything.

Deterministic and cheap on purpose -- pure stdlib string work over the file set already
stored in `site_versions.files`, no API call and no browser. Around 300 tokens for a
four-page site, which is a rounding error against the ~9,000 an edit already costs.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

from worker.codegen.css_values import style_digest

# Elements that mark a distinct region of a page. Anything else is detail the parser does
# not need in order to say "the section at the top of the home page".
BLOCK_TAGS = frozenset({"header", "nav", "main", "section", "footer", "article", "aside"})
HEADING_TAGS = frozenset({"h1", "h2", "h3"})
# Classes worth counting: they are the repeated building blocks an owner refers to
# ("the cards", "the FAQ", "the steps").
COUNTED_CLASSES = ("card", "faq-item", "step", "gallery-image", "contact-item")

MAX_HEADING_CHARS = 60
MAX_BLOCKS_PER_PAGE = 14


def _clip(text: str, limit: int = MAX_HEADING_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _PageOutline(HTMLParser):
    """Walks a page and records its regions, headings, pictures and buttons."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._open: list[dict] = []
        self._capture: str | None = None  # "heading" or "label" while inside one
        self._buffer: list[str] = []

    def _current(self) -> dict | None:
        return self._open[-1] if self._open else None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()

        if tag in BLOCK_TAGS:
            block = {"tag": tag, "classes": classes, "headings": [], "images": [],
                     "buttons": [], "nav": [], "counts": {},
                     "inline_bg": "background-image" in (attr.get("style") or "")}
            self.blocks.append(block)
            self._open.append(block)
            return

        block = self._current()
        if block is None:
            return

        if tag in HEADING_TAGS:
            self._capture, self._buffer = "heading", []
        elif tag == "img":
            # The tail of the URL is enough to tell two pictures apart without pasting a
            # 150-character Supabase URL into the prompt.
            src = attr.get("src") or ""
            block["images"].append({
                "class": " ".join(classes) or "(no class)",
                "src_tail": src.rsplit("/", 1)[-1][-24:] if src else "(empty src)",
            })
        elif tag in ("a", "button") and any(c.startswith("btn") for c in classes):
            self._capture, self._buffer = "label", []
        elif tag == "a" and "nav-link" in classes:
            # The menu is one of the most commonly edited parts of a site, so its labels
            # are worth the handful of tokens.
            self._capture, self._buffer = "nav", []

        for name in COUNTED_CLASSES:
            if name in classes:
                block["counts"][name] = block["counts"].get(name, 0) + 1

    def handle_endtag(self, tag: str) -> None:
        block = self._current()
        if block is not None and self._capture:
            if tag in HEADING_TAGS or tag in ("a", "button"):
                text = _clip("".join(self._buffer))
                if text:
                    block[{"heading": "headings", "label": "buttons",
                           "nav": "nav"}[self._capture]].append(text)
                self._capture, self._buffer = None, []

        if tag in BLOCK_TAGS and self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)


def _describe_block(block: dict) -> str:
    name = block["tag"]
    if block["classes"]:
        name += "." + ".".join(block["classes"])
    parts = [name]

    if block["headings"]:
        parts.append(" / ".join(f'"{h}"' for h in block["headings"][:2]))
    for img in block["images"][:3]:
        parts.append(f'image [{img["class"]}] …{img["src_tail"]}')
    if block["inline_bg"]:
        parts.append("has a background image set on it")
    for cls, count in block["counts"].items():
        parts.append(f"{count}x .{cls}")
    if block["nav"]:
        parts.append("menu: " + ", ".join(block["nav"][:6]))
    if block["buttons"]:
        parts.append("buttons: " + ", ".join(f'"{b}"' for b in block["buttons"][:3]))
    return " | ".join(parts)


def _is_informative(block: dict) -> bool:
    """Drop wrapper elements that carry no information -- a bare `main` or an empty
    `section` tells the parser nothing and costs tokens on every page."""
    return bool(
        block["headings"] or block["images"] or block["buttons"] or block["nav"]
        or block["counts"] or block["inline_bg"] or block["classes"]
    )


def outline_page(html: str) -> list[str]:
    parser = _PageOutline()
    try:
        parser.feed(html)
    except Exception:
        # A malformed page must never break editing -- an empty outline just means the
        # parser falls back to the behaviour it had before this existed.
        return []
    keep = [b for b in parser.blocks if _is_informative(b)]
    return [_describe_block(b) for b in keep[:MAX_BLOCKS_PER_PAGE]]


_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CLASS_RE = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")


def stylesheet_classes(css: str) -> list[str]:
    """Every class the stylesheet actually defines a rule for, in source order."""
    seen: list[str] = []
    for chunk in _COMMENT_RE.sub("", css).split("}"):
        selector = chunk.rsplit("{", 1)[0] if "{" in chunk else ""
        for name in _CLASS_RE.findall(selector):
            if name not in seen:
                seen.append(name)
    return seen


def outline_site(files: dict[str, str] | None) -> str:
    """Render the whole file set as a short map the edit parser can read.

    Returns "" when there are no stored files (a site that has never been built), which
    leaves the prompt exactly as it was before.
    """
    if not files:
        return ""

    pages = {name: outline_page(files[name])
             for name in sorted(n for n in files if n.endswith(".html"))}

    # The header, menu and footer are identical on every page, so listing them four times
    # is pure repetition. Hoisting them also states the fact that matters for editing:
    # changing one means changing all of them.
    shared: list[str] = []
    if len(pages) > 1:
        first = pages[next(iter(pages))]
        shared = [row for row in first if all(row in rows for rows in pages.values())]

    lines: list[str] = []
    if shared:
        lines.append("\nOn every page (change one, change all):")
        lines.extend(f"  {row}" for row in shared)

    for name, rows in pages.items():
        unique = [row for row in rows if row not in shared]
        lines.append(f"\n{name}")
        if unique:
            lines.extend(f"  {row}" for row in unique)
        else:
            lines.append("  (nothing beyond the shared parts above)")

    css = files.get("style.css")
    if css:
        classes = stylesheet_classes(css)
        shown = ", ".join(f".{c}" for c in classes[:60])
        more = f" (+{len(classes) - 60} more)" if len(classes) > 60 else ""
        lines.append(f"\nstyle.css defines rules for: {shown}{more}")

    # The values, not just the names. Without these the parser cannot tell a change
    # it is about to ask for from one that was already made -- see css_values.py for
    # the loop that put a real owner through six identical requests over two days.
    digest = style_digest(files)
    if digest:
        lines.append("\n" + digest)

    return "\n".join(lines)
