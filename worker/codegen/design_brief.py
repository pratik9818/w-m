"""Decide how *this* site should look, once, before anything is written.

Every site this bot built looked like the same site: system fonts, a blue accent, white
cards in a grid, in the same order, for a plumber and a crypto token alike. Three causes,
and this module addresses two of them.

The stylesheet and the pages are written by separate concurrent calls, and the only thing
they shared was a list of class names. So the stylesheet was written *blind* -- six fields
of business data, not one word of the page it was styling -- and a designer who cannot see
the content writes defensively generic CSS. And the whole art direction was three
sentences per theme, one of which ("minimal and clean: white background, one confident
accent, sans-serif throughout") is a description of the default template.

The brief replaces both. One short call picks a palette, a typeface pairing and a
signature device grounded in this particular business, and the same brief is then handed
to every call in the build. It costs about a tenth of what one page costs.

Nothing here is trusted on the model's word: fonts are checked against a list of families
that really exist on Google Fonts, contrast is computed and repaired rather than requested,
and any failure falls back to a hand-picked brief for the theme so a build never dies for
want of art direction.
"""
from __future__ import annotations

import logging
import re

from bot_api.services.llm_client import LLMCallFailed, call_forced_tool

logger = logging.getLogger(__name__)

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Families that are really on Google Fonts, each with the stack to fall back to. A
# hallucinated family name is not a small error: the link 404s, the page silently renders
# in Times New Roman, and the site looks worse than it did with system fonts.
DISPLAY_FONTS = {
    "Fraunces": "serif", "Playfair Display": "serif", "DM Serif Display": "serif",
    "Instrument Serif": "serif", "Libre Baskerville": "serif", "Cormorant Garamond": "serif",
    "Lora": "serif", "Newsreader": "serif", "Spectral": "serif", "Merriweather": "serif",
    "Crimson Pro": "serif", "IBM Plex Serif": "serif", "Source Serif 4": "serif",
    "Bricolage Grotesque": "sans-serif", "Space Grotesk": "sans-serif", "Archivo": "sans-serif",
    "Sora": "sans-serif", "Outfit": "sans-serif", "Manrope": "sans-serif", "Syne": "sans-serif",
    "Epilogue": "sans-serif", "Figtree": "sans-serif", "Plus Jakarta Sans": "sans-serif",
    "Work Sans": "sans-serif", "Public Sans": "sans-serif", "Oswald": "sans-serif",
    "Anton": "sans-serif", "Bebas Neue": "sans-serif", "Unbounded": "sans-serif",
    "Chivo": "sans-serif", "IBM Plex Sans": "sans-serif", "Rubik": "sans-serif",
    "Poppins": "sans-serif", "Josefin Sans": "sans-serif", "Raleway": "sans-serif",
}
BODY_FONTS = {
    "Inter": "sans-serif", "Work Sans": "sans-serif", "Public Sans": "sans-serif",
    "Karla": "sans-serif", "Figtree": "sans-serif", "Manrope": "sans-serif",
    "IBM Plex Sans": "sans-serif", "Rubik": "sans-serif", "Nunito Sans": "sans-serif",
    "Open Sans": "sans-serif", "Libre Franklin": "sans-serif", "Atkinson Hyperlegible": "sans-serif",
    "Source Sans 3": "sans-serif", "Epilogue": "sans-serif", "Archivo": "sans-serif",
    "Source Serif 4": "serif", "Lora": "serif", "Spectral": "serif", "Newsreader": "serif",
    "Merriweather": "serif", "Crimson Pro": "serif", "IBM Plex Serif": "serif",
}

# Where the model is asked to start from, not where it must end up. Each is a direction
# with a point of view, unlike the three sentences these replace.
THEME_MOODS = {
    "classic": (
        "Established and trustworthy. Think a good printed brochure or a signwritten "
        "shopfront: a serif for headings, warm paper-like ground rather than pure white, "
        "an accent with some depth to it (ink blue, forest, oxblood, bronze)."
    ),
    "modern": (
        "Current and considered, the way a well-funded product site looks this year. "
        "Confident type at large sizes, real negative space, a restrained palette with "
        "one accent that does all the work, and edges and shadows used sparingly."
    ),
    "bold": (
        "Loud on purpose. High contrast ground, oversized display type, one saturated "
        "colour used at full strength across whole bands, tight tracking on headings."
    ),
}

# Hand-picked, contrast-checked, used when the call fails or comes back unusable. A build
# must never fail for want of art direction.
FALLBACK_BRIEFS = {
    "classic": {
        "concept": "A long-established local business that does careful work and stands behind it.",
        "display_font": "Fraunces", "body_font": "Source Serif 4",
        "bg": "#f6f2ea", "surface": "#fffdf8", "ink": "#241f1a", "accent": "#7c2d2d",
        "signature": "Thin rules under every section heading, like a printed brochure.",
    },
    "modern": {
        "concept": "A precise, current business that makes the next step obvious.",
        "display_font": "Bricolage Grotesque", "body_font": "Inter",
        "bg": "#f7f8f7", "surface": "#ffffff", "ink": "#16191c", "accent": "#1f6f5c",
        "signature": "One oversized headline per page, everything else quiet around it.",
    },
    "bold": {
        "concept": "A business with an edge that is not trying to please everyone.",
        "display_font": "Anton", "body_font": "Work Sans",
        "bg": "#121212", "surface": "#1c1c1c", "ink": "#f2f2f0", "accent": "#ff5c2b",
        "signature": "Full-width colour bands between sections, hard edges, no rounding.",
    },
}

DESIGN_BRIEF_TOOL = {
    "name": "design_brief",
    "description": "The art direction for one small business website: palette, typefaces, and the one device that makes it recognisable.",
    "parameters": {
        "type": "object",
        "properties": {
            "concept": {
                "type": "string",
                "description": "One sentence on what this site should feel like to the visitor, "
                "specific to THIS business and what it sells. Not 'clean and professional'.",
            },
            "display_font": {
                "type": "string",
                "description": "Heading typeface. Must be exactly one of the DISPLAY list given above.",
            },
            "body_font": {
                "type": "string",
                "description": "Body typeface. Must be exactly one of the BODY list given above, "
                "and must contrast with the heading face rather than match it.",
            },
            "bg": {"type": "string", "description": "Page background, as #rrggbb."},
            "surface": {"type": "string", "description": "Cards and raised panels, as #rrggbb. Close to bg, not identical."},
            "ink": {"type": "string", "description": "Body text colour, as #rrggbb. Must be readable on bg."},
            "accent": {"type": "string", "description": "The single accent, as #rrggbb. Buttons, links, bands."},
            "signature": {
                "type": "string",
                "description": "One distinctive visual device used consistently across the site "
                "(e.g. 'numbered steps as oversized outlined numerals', 'every section "
                "separated by a full-width colour band'). One sentence, and it must be "
                "achievable in CSS with no images and no JavaScript.",
            },
        },
        "required": ["concept", "display_font", "body_font", "bg", "surface", "ink", "accent", "signature"],
    },
}

PROMPT_TEMPLATE = """You are the art director for a small business's website. Before anyone writes a line of it, you decide how it should look.

The business:

```json
{spec_json}
```

Mood the owner picked ({theme}): {mood}
{extra}
## What makes this worth doing

Every site this system has produced so far looks the same: system fonts, a blue accent, white cards in a grid. Your job is to make THIS one look like it belongs to THIS business and no other. Someone should be able to tell the trade from the design alone, before reading a word.

- Ground the palette in the business's own world -- what its materials, tools, room or product actually look like. A tattoo studio and a chai stall should not share a colour.
- Pair the two typefaces deliberately, so they contrast: a characterful heading face against a plain, highly readable body face. Never pick the same family for both.
- Avoid what everything already looks like: pure white with a blue accent, purple-to-blue gradients, warm cream with terracotta, and rounded cards with a coloured bar down one side. If your first instinct is one of those, pick again.
- The signature device is what stops the site being a template. Make it specific and cheap to build: it has to work in plain CSS, with no images and no JavaScript.

## Constraints

- `display_font` must be exactly one of: {display_fonts}
- `body_font` must be exactly one of: {body_fonts}
- Colours must be `#rrggbb`. Body text must be clearly readable on the background -- dark text on a light ground or light text on a dark one, never a mid grey on a mid grey.

Call `design_brief` now."""


def _clamp_hex(value: str, fallback: str) -> str:
    value = str(value or "").strip()
    return value.lower() if _HEX_RE.match(value) else fallback


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    hex_colour = hex_colour.lstrip("#")
    return tuple(int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(hex_colour: str) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1.0 (identical) to 21.0 (black on white)."""
    first, second = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def _mix(a: str, b: str, amount: float) -> str:
    """`amount` of the way from a to b, as #rrggbb."""
    parts = (
        round((x + (y - x) * amount) * 255)
        for x, y in zip(_rgb(a), _rgb(b))
    )
    return "#" + "".join(f"{value:02x}" for value in parts)


def _readable_on(background: str) -> str:
    """Black or white, whichever can actually be read on this colour."""
    return "#111111" if contrast("#111111", background) >= contrast("#ffffff", background) else "#ffffff"


def normalize(raw: dict, theme: str) -> dict:
    """Turn a model's answer into a brief that is guaranteed usable.

    Everything checkable is checked here rather than asked for in the prompt: a font that
    does not exist falls back to the theme's, and text that would not be readable on its
    background is replaced instead of published. A rule that lives only in a prompt is a
    hope.
    """
    fallback = FALLBACK_BRIEFS.get(theme, FALLBACK_BRIEFS["modern"])

    display = str(raw.get("display_font") or "").strip()
    body = str(raw.get("body_font") or "").strip()
    if display not in DISPLAY_FONTS:
        logger.info("design brief: unknown display font %r, using %s", display, fallback["display_font"])
        display = fallback["display_font"]
    if body not in BODY_FONTS:
        logger.info("design brief: unknown body font %r, using %s", body, fallback["body_font"])
        body = fallback["body_font"]
    if body == display:
        body = fallback["body_font"] if fallback["body_font"] != display else "Inter"

    bg = _clamp_hex(raw.get("bg"), fallback["bg"])
    surface = _clamp_hex(raw.get("surface"), fallback["surface"])
    ink = _clamp_hex(raw.get("ink"), fallback["ink"])
    accent = _clamp_hex(raw.get("accent"), fallback["accent"])

    # Readability is not negotiable and not worth a second model call: if the chosen ink
    # cannot be read on the chosen ground, take the nearest thing that can.
    if contrast(ink, bg) < 4.5:
        logger.info("design brief: ink %s on %s scored %.1f, replacing", ink, bg, contrast(ink, bg))
        ink = _readable_on(bg)
    if contrast(surface, bg) < 1.03:
        surface = _mix(bg, ink, 0.05)

    return {
        "concept": str(raw.get("concept") or fallback["concept"]).strip()[:300],
        "signature": str(raw.get("signature") or fallback["signature"]).strip()[:300],
        "display_font": display,
        "display_stack": DISPLAY_FONTS[display],
        "body_font": body,
        "body_stack": BODY_FONTS[body],
        "bg": bg,
        "surface": surface,
        "ink": ink,
        "muted": _mix(ink, bg, 0.45),
        "accent": accent,
        # Computed, never asked for: "make sure text on the accent band is readable" was a
        # prompt rule, and prompt rules do not hold.
        "accent_ink": _readable_on(accent),
        "border": _mix(bg, ink, 0.15),
    }


def fallback_brief(theme: str) -> dict:
    return normalize(dict(FALLBACK_BRIEFS.get(theme, FALLBACK_BRIEFS["modern"])), theme)


def font_link(brief: dict) -> str:
    """The Google Fonts <link> for this brief's two families."""
    families = "&".join(
        f"family={family.replace(' ', '+')}:wght@{weights}"
        for family, weights in (
            (brief["display_font"], "400;600;700"),
            (brief["body_font"], "400;600"),
        )
    )
    return f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?{families}&display=swap">'


def brief_block(brief: dict) -> str:
    """The art direction, in the form both the stylesheet and the pages calls receive."""
    return f"""## The design direction for this site

**Concept:** {brief["concept"]}

**Signature device:** {brief["signature"]} — carry it through the whole site, not just one section.

**Palette — these are decided. Use exactly these values, and define them as custom properties on `:root`:**

- background: `{brief["bg"]}`
- raised surfaces (cards, panels): `{brief["surface"]}`
- body text: `{brief["ink"]}`
- secondary text: `{brief["muted"]}`
- borders and rules: `{brief["border"]}`
- the one accent: `{brief["accent"]}`
- text on top of the accent: `{brief["accent_ink"]}` (use this and nothing else on an accent background)

**Typefaces — already loaded in every page's head. Use them and no others:**

- headings and display: `"{brief["display_font"]}", {brief["display_stack"]}`
- body text: `"{brief["body_font"]}", {brief["body_stack"]}`
"""


async def make_design_brief(spec: dict, spec_json: str) -> tuple[dict, dict | None]:
    """Choose the art direction for this site. Returns (brief, usage_or_None).

    Never raises: a build without a brief is a worse outcome than a build with the
    theme's fallback, so any failure here is logged and the fallback is used.
    """
    theme = spec.get("theme") or "classic"
    extra = ""
    if spec.get("extra_instructions"):
        extra = (
            f"\nThe owner has also asked, in their own words, for: "
            f"{spec['extra_instructions']}\nHonour that above the mood.\n"
        )

    prompt = PROMPT_TEMPLATE.format(
        spec_json=spec_json,
        theme=theme,
        mood=THEME_MOODS.get(theme, THEME_MOODS["classic"]),
        extra=extra,
        display_fonts=", ".join(sorted(DISPLAY_FONTS)),
        body_fonts=", ".join(sorted(BODY_FONTS)),
    )

    try:
        raw, usage = await call_forced_tool(prompt, [DESIGN_BRIEF_TOOL])
    except (LLMCallFailed, Exception) as exc:  # noqa: B014 - deliberate catch-all
        logger.warning("design brief failed, using the %s fallback: %s", theme, exc)
        return fallback_brief(theme), None

    brief = normalize(raw, theme)
    logger.info(
        "design.brief theme=%s fonts=%s/%s accent=%s",
        theme, brief["display_font"], brief["body_font"], brief["accent"],
    )
    return brief, usage


def design_tokens_css(brief: dict) -> str:
    """The palette and typefaces as real CSS, written by us and not by the model.

    The prompt asks for these too, but a prompt is a hope: a stylesheet that forgets to set
    a font family leaves the site in system-ui, which is exactly the default look this
    whole change exists to escape. Emitted at the top of the generated half, so the
    model's own rules still come after it and win wherever they disagree -- and so a later
    edit can still see and change any of it.
    """
    return f""":root {{
  --font-display: "{brief["display_font"]}", {brief["display_stack"]};
  --font-body: "{brief["body_font"]}", {brief["body_stack"]};
  --color-bg: {brief["bg"]};
  --color-surface: {brief["surface"]};
  --color-ink: {brief["ink"]};
  --color-muted: {brief["muted"]};
  --color-border: {brief["border"]};
  --color-accent: {brief["accent"]};
  --color-accent-ink: {brief["accent_ink"]};
}}

body {{
  font-family: var(--font-body);
  background-color: var(--color-bg);
  color: var(--color-ink);
}}

h1, h2, h3,
.hero-title, .section-title, .card-title, .cta-title, .step-title, .footer-title, .logo-text {{
  font-family: var(--font-display);
}}
"""
