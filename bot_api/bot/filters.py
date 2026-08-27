import re

from aiogram.types import Message

# The words that release a change. Shared rather than copied because two handlers now gate
# on it -- building a site and editing one -- and a "yes" that works in one conversation
# and not the other is the kind of difference an owner reads as the bot ignoring them.
_AFFIRMATIONS = {
    "yes", "yep", "yeah", "sure", "go ahead", "looks good", "perfect", "publish it",
    "do it", "confirm", "ok", "okay", "y", "yes please", "build it", "correct",
}
_PUNCT_RE = re.compile(r"[.!?]+$")


def is_affirmation(text: str) -> bool:
    """Is this the owner saying yes, and nothing else?

    Deliberately an exact match against a short list rather than anything cleverer. This
    decides whether a site gets rewritten, so "yes, but change the phone number first"
    must not read as a yes -- it falls through and is handled as the correction it is.
    """
    normalized = _PUNCT_RE.sub("", (text or "").strip().lower())
    return normalized in _AFFIRMATIONS


_DECLINES = {
    "no", "nope", "nah", "never mind", "nevermind", "cancel", "forget it", "no thanks",
    "no thank you", "stop", "leave it", "don't", "dont",
}


def is_declining(text: str) -> bool:
    """Is this the owner backing out, and nothing else?

    The mirror of `is_affirmation`, and exact for the same reason: "no, put it at the top"
    is a placement, not a cancellation, and reading it as one would throw away the picture
    they just sent along with the instruction they just gave.
    """
    normalized = _PUNCT_RE.sub("", (text or "").strip().lower())
    return normalized in _DECLINES


def has_text(message: Message) -> bool:
    """True for messages with non-empty text. Safer than F.text.len() > 0, which
    raises if a non-text message (e.g. a photo) arrives while a text state is active."""
    return bool(message.text)
