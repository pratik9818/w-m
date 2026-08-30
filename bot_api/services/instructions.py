"""Is this message telling the bot to change the site?

Two gates run before the edit pipeline reads anything with a model: "how many people
visited?" and "show me my enquiries" are both answered from a lookup, and paying a model
to discover that is paying for a worse version of a column lookup. Both gates need the
same protection -- an owner asking for a change must never fall into either -- and both
used to carry their own copy of it, with different verb lists. This is that check, once.

The two mistakes are not the same size, and this leans accordingly.

A question misread as an edit costs one model call and still comes out right: the parser
reads it, answers `not_a_change`, and the assistant handles it properly a moment later.
An edit misread as a question is a dead end. The change is dropped, the owner is handed a
visitor chart, and nothing in the reply admits that anything was missed.

It happened live. "Center form in desktop view and please make text color black" was
answered with traffic figures three times running, including once after the owner wrote
"I don't want this". Two things let it through: "desktop view" contains "view", and the
guard only recognised an instruction when the message *opened* with one of about fifteen
verbs -- "center" not among them.

So the imperative is looked for anywhere in the message rather than at the front, and the
verb list is long rather than tidy. Both choices push the doubtful case toward the edit
pipeline, which recovers, and away from the lookup, which does not.
"""
import re

# Imperatives an owner actually writes. Nouns that double as verbs are deliberately left
# out -- "place", "link", "drop" and "colour" appear far more often as things than as
# instructions, and each one would send a genuine question down the longer path.
_VERBS = (
    "add|put|insert|create|build|design|"
    "make|change|update|edit|modify|adjust|tweak|fix|correct|improve|"
    "remove|delete|hide|"
    "set|move|shift|swap|replace|rename|reorder|rearrange|"
    "center|centre|align|resize|increase|decrease|reduce|enlarge|shrink|widen|"
    "shorten|lengthen|bold|underline|highlight|capitalise|capitalize|"
    "write|rewrite|reword|translate|upload|attach"
)

# "show me a form" is an instruction; "show me my visitors" is a question. The article is
# the entire difference, so these carry it instead of matching the bare verb.
_ASK_FOR_ONE = r"(?:show|give)\s+me\s+(?:a|an|another|one)\b"

_INSTRUCTION_RE = re.compile(rf"\b(?:{_VERBS})\b|{_ASK_FOR_ONE}", re.IGNORECASE)


def looks_like_an_instruction(text: str) -> bool:
    """True when the message asks for something to be done to the site.

    A trailing question mark overrides the verb: "can you make the header blue?" is asking
    what is possible, and "how many people viewed the page I added?" is a question about
    visitors that merely mentions adding. Both are questions, whatever verb is in them.
    """
    body = (text or "").strip()
    if not body or body.endswith("?"):
        return False
    return bool(_INSTRUCTION_RE.search(body))
