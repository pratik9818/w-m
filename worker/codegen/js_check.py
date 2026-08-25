"""Syntax check for the JavaScript the model writes into a page.

Sites may ship script now, and the first real build that did died in the sandbox on
`Unexpected token ')'` -- a 90-second container spun up, loaded, and thrown away over a
stray bracket. A browser is the only thing that can truly parse JavaScript, but it is by
far the most expensive way to find out that a paren does not close.

So this is deliberately not a parser. It answers one narrow question -- are the brackets,
strings and comments balanced -- because that is what the failures actually look like when
a small model writes a function it never ran. Anything subtler is left to the sandbox,
which still runs and still has the final say.

The bias is towards saying nothing. A false positive fails a build that would have worked,
which is worse than the 90 seconds a false negative costs, so every ambiguous construct
makes this give up rather than guess.
"""
import re

# Inline scripts only. A <script src=...> is somebody else's file and is checked, if at
# all, by whether the browser can fetch it.
INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL
)
CLOSERS = {")": "(", "]": "[", "}": "{"}
MAX_REPORTED = 5


def _scan(script: str) -> tuple[str, bool, str | None]:
    """Strip strings and comments in one pass.

    Returns (code_only, saw_slash, unterminated). `saw_slash` flags a `/` that survived
    outside a string or comment: it is either division or the start of a regular
    expression literal, and a regex may legally contain an unbalanced bracket (`/[(/`).
    Telling the two apart needs to know whether an expression can end here, which is real
    parsing -- so the caller simply stops trusting the bracket count when it is set.
    """
    out: list[str] = []
    state: str | None = None
    quote = ""
    i, n = 0, len(script)
    saw_slash = False

    while i < n:
        ch = script[i]
        if state is None:
            if ch in "\"'`":
                state, quote = "string", ch
            elif script.startswith("//", i):
                state = "line"
                i += 2
                continue
            elif script.startswith("/*", i):
                state = "block"
                i += 2
                continue
            else:
                if ch == "/":
                    saw_slash = True
                out.append(ch)
            i += 1
        elif state == "string":
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                state = None
            # A newline inside a plain quote is an unterminated string; backticks allow it.
            elif ch == "\n" and quote != "`":
                return "".join(out), saw_slash, f"unterminated {quote}...{quote} string"
            i += 1
        elif state == "line":
            if ch == "\n":
                state = None
            i += 1
        else:  # block comment
            if script.startswith("*/", i):
                state = None
                i += 2
                continue
            i += 1

    unterminated = None
    if state == "string":
        unterminated = f"unterminated {quote}...{quote} string"
    elif state == "block":
        unterminated = "unterminated /* comment"
    return "".join(out), saw_slash, unterminated


def _bracket_problem(code: str) -> str | None:
    stack: list[str] = []
    for ch in code:
        if ch in "([{":
            stack.append(ch)
        elif ch in CLOSERS:
            if not stack or stack[-1] != CLOSERS[ch]:
                return f"unexpected '{ch}'"
            stack.pop()
    if stack:
        return f"'{stack[-1]}' is never closed"
    return None


def script_problems(script: str) -> list[str]:
    """Problems in one script's source; empty means nothing detectable is wrong."""
    code, saw_slash, unterminated = _scan(script)
    if unterminated:
        return [unterminated]
    if saw_slash:
        # Could be a regex literal, whose contents are not brackets. Say nothing.
        return []
    problem = _bracket_problem(code)
    return [problem] if problem else []


def js_problems(html: str) -> list[str]:
    """Problems across every inline script in one page."""
    found: list[str] = []
    for match in INLINE_SCRIPT_RE.finditer(html):
        found += script_problems(match.group(1))
    return found[:MAX_REPORTED]
