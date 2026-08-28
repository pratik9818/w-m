"""Turn one failure's message into the name of the *class* of failure it belongs to.

The point of a signature is that many incidents collapse into one row. Six real rows in
edit_log read:

    style change rejected: applied .hero margin-bottom: 2rem but the stylesheet does not say so
    style change rejected: applied .section margin-top: 2rem but the stylesheet does not say so
    style change rejected: applied .hero padding: 96px but the stylesheet does not say so
    ...

Read one at a time, each looks like a one-off about a particular selector on a particular
site. Read as one signature -- "set_style: verification mismatch, 6 times, 3 sites" -- it
is obviously a single defect, and it was: a CSS scanner that stopped reading at the first
apostrophe in a comment. That defect went unnoticed for days because nothing was grouping.

Every signature also carries a *fault*, which is the more important half. A failure whose
deterministic postcondition was breached is a code defect and belongs to whoever writes
the code. A failure where the code did exactly as instructed and the owner still did not
get what they meant is a model problem. Treating the first kind as the second is how a
system ends up writing prompt instructions to work around a broken parser.
"""
from __future__ import annotations

import re

# Who is at fault, which decides which queue a failure lands in.
FAULT_CODE = "code"          # a deterministic postcondition was breached -- a real bug
FAULT_MODEL = "model"        # the code did as it was told; the reading of the owner was wrong
FAULT_TIMING = "timing"      # neither: the request arrived at a bad moment
FAULT_NONE = "none"          # not a failure at all
FAULT_UNKNOWN = "unknown"    # unrecognised -- surfaced so it can be classified, never hidden

# Matched in order; first hit wins. Anything unmatched falls through to _generic() rather
# than being dropped, because an unrecognised failure is exactly the one worth seeing.
_RULES: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"^style change rejected", re.I),
        "set_style: applied but not present in the stylesheet",
        FAULT_CODE,
    ),
    (
        re.compile(r"^rejected: structural request sent to", re.I),
        "guard: structural request routed to a patch",
        FAULT_MODEL,
    ),
    (
        # Not a fault in anything. The owner asked while a build was running, which the
        # bot correctly refused. Counted so it can be told apart from real failures --
        # without it, a busy afternoon looks like a spike in defects.
        re.compile(r"^rejected: business busy", re.I),
        "guard: another change was already running",
        FAULT_TIMING,
    ),
    (
        re.compile(r"^edit parsing failed", re.I),
        "parse: could not read the message",
        FAULT_MODEL,
    ),
    (
        # Blamed on the code, not the model. The model named two real files; it just
        # wrote them as "index.html, style.css" rather than as an array, and our own
        # normaliser iterated that string one character at a time and found nothing. The
        # owner was asked which page they meant, immediately after saying "yes".
        re.compile(r"^patch_site missing instruction or valid targets", re.I),
        "patch_site: target list came out empty",
        FAULT_CODE,
    ),
    (
        re.compile(r"^daily limit reached", re.I),
        "quota: daily model limit reached",
        FAULT_TIMING,
    ),
    (
        re.compile(r"^answered as a question", re.I),
        "assistant: answered a question",
        FAULT_NONE,
    ),
    (
        re.compile(r"^asked:", re.I),
        "clarify: asked the owner a question",
        FAULT_NONE,
    ),
]

# The variable parts of an error message -- the bits that differ between two incidents of
# the same defect and would otherwise split one row into ten.
_SELECTOR_RE = re.compile(r"(?<![\w.])[.#][a-zA-Z][\w-]*")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?(?:px|rem|em|%|vh|vw|s|ms)?\b")
_QUOTED_RE = re.compile(r"""(['"]).*?\1""")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_PARENS_RE = re.compile(r"\([^)]*\)")

MAX_SIGNATURE_CHARS = 90


def _generic(error: str) -> str:
    """A stable name for an error nobody has written a rule for yet."""
    text = _UUID_RE.sub("<id>", error)
    text = _QUOTED_RE.sub("<text>", text)
    text = _PARENS_RE.sub("(...)", text)
    text = _SELECTOR_RE.sub("<sel>", text)
    text = _NUMBER_RE.sub("<n>", text)
    text = " ".join(text.split()).lower()
    return text[:MAX_SIGNATURE_CHARS]


def classify(error: str | None) -> tuple[str | None, str]:
    """Return (signature, fault) for one edit_log error string.

    A row with no error has no signature -- there is nothing to group it with.
    """
    if not error or not error.strip():
        return None, FAULT_NONE

    text = error.strip()
    for pattern, signature, fault in _RULES:
        if pattern.search(text):
            return signature, fault

    return _generic(text), FAULT_UNKNOWN


def is_a_real_failure(fault: str) -> bool:
    """Should this count towards the defect numbers?

    Timing and clarifying questions are excluded deliberately. A bot that asks a good
    question has not failed, and an owner who typed while a build was running has not
    either -- counting both would bury the handful of rows that are genuinely broken.
    """
    return fault in (FAULT_CODE, FAULT_MODEL, FAULT_UNKNOWN)
