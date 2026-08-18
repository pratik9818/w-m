"""Well-formedness check for generated HTML.

Deliberately operates on the raw source bytes rather than the rendered DOM. Every
browser-based check we have is blind to this class of defect by design: Chromium
silently repairs malformed markup, so `page_loads` and `content_present` pass happily
on a document with mismatched tags. A real instance shipped to production this session
-- `<h2 class="section-title">What We Do</h>` was live and all nine sandbox checks
passed it.

Uses only stdlib html.parser, so it costs nothing and needs no new dependency.
"""
from html.parser import HTMLParser

# Elements that never have a closing tag.
VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
# Closing these is optional in HTML5, so an unclosed one is not a defect.
OPTIONAL_END_TAG = frozenset({"p", "li", "dt", "dd", "option", "thead", "tbody", "tfoot", "tr", "td", "th"})
MAX_REPORTED = 10


class _WellFormednessParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag: str, attrs) -> None:
        pass  # self-closing, nothing to match

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            self.problems.append(f"line {self.getpos()[0]}: </{tag}> closes a void element")
            return

        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth][0] == tag:
                # Anything still open above the match was never closed.
                for orphan, line in self.stack[depth + 1:]:
                    if orphan not in OPTIONAL_END_TAG:
                        self.problems.append(f"line {line}: <{orphan}> is never closed")
                del self.stack[depth:]
                return

        self.problems.append(f"line {self.getpos()[0]}: </{tag}> has no matching opening tag")


def html_problems(html: str) -> list[str]:
    """Return a list of well-formedness problems; empty means the document is sound."""
    parser = _WellFormednessParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # a parser blow-up is itself a defect worth reporting
        parser.problems.append(f"parse error: {exc}")

    problems = list(parser.problems)
    problems += [
        f"line {line}: <{tag}> is never closed"
        for tag, line in parser.stack
        if tag not in OPTIONAL_END_TAG
    ]
    return problems[:MAX_REPORTED]
