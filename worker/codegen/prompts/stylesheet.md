$shared

## Your task in this response: the stylesheet only

Write the complete shared `style.css` for this business's four-page website. You are not
writing any HTML in this response — the pages are generated separately against the shared class
contract above, so style exactly those class names.

Reply with ONLY the file below, no explanation before or after. The marker lines must appear
alone on their own line, exactly as written:

===FILE: style.css===
<the complete stylesheet>
===END===

## Design quality bar

The result must look like a designer made it for this one business. The failure to avoid is
not ugliness — it is blandness: a white page, a blue accent, evenly spaced grey cards, the
same site every other small business has.

- **Build the design direction above, exactly.** The palette and the two typefaces are
  decided; your job is to use them well, not to substitute your own. Set the spacing scale,
  radii and shadows as custom properties on `:root` alongside them.
- **Make the type do the work.** Set a real scale with clear jumps rather than three sizes a
  few pixels apart — a hero heading should be several times the body size. Use `clamp()` so it
  holds up from phone to desktop. Give headings tighter line-height and letter-spacing than
  body text, and keep body text near 65-75 characters per line.
- **Build the signature device from the direction above** and repeat it across the site. It is
  what makes this design belong to this business.
- Give the page clear visual hierarchy — a distinct header, a hero with real presence, section
  bands that alternate background via `section-alt`, and generous vertical rhythm between
  sections. Whitespace between sections should be large enough to feel deliberate.
- **Do not make everything the same shape.** Vary the rhythm: a full-bleed band against a
  narrow column, one oversized element against several small ones, an asymmetric hero rather
  than everything centred.
- Style every group in the class contract as a designed component: `card` with padding and a
  subtle shadow, `step-number` with a visually distinct numbered treatment, `faq-item` as a
  styled question block, `cta-band` as a visually distinct full-width band, and `site-footer`
  as a structured multi-column layout.
- Include hover and focus states for `nav-link`, `btn`, `btn-primary`, `btn-secondary`, links,
  and `card`.
- **Design the motion, don't leave it to the pages.** Transitions on every interactive
  element, and `@keyframes` where the design wants them. The pages may add JavaScript that
  toggles state classes — write rules for the obvious ones (`.is-open` on the mobile nav,
  `.is-visible` for a scroll-reveal, `.is-active` on a tab or a carousel dot) so that
  behaviour lands on a styled element rather than an unstyled one. Anything you hide for a
  reveal must be hidden by one of those JS-added classes, never by default, or the page is
  blank without JavaScript.
- Honour `@media (prefers-reduced-motion: reduce)` by cutting transitions and animations
  down to nothing.
- Fully responsive using CSS only — flexbox and grid plus media queries. The nav must stay
  usable on mobile without any JavaScript (wrapping or stacking is fine).

  Write for these five widths by name. "Three media queries covering large, tablet and
  mobile" was too vague: it produced stylesheets whose largest breakpoint was 1024px, so
  every real laptop and monitor fell through into untested territory and owners reported
  sites that were fine on a phone and inconsistent on a laptop.

  | Width | What it is | What the layout must do |
  |---|---|---|
  | 390px | a phone | Single column throughout. Nothing side by side. |
  | 768px | a tablet | Two columns at most. |
  | 1024px | a small laptop | Two or three columns. This is **not** your largest size. |
  | 1280px | a laptop | The full design. |
  | 1920px+ | a desktop monitor | Still the full design, centred, not stretched. |

  Two absolute rules:

  - **Nothing may cause sideways scrolling at any width from 320px to 2560px.** The build
    measures this at five widths and fails if the page scrolls sideways at any of them.
    The usual causes are a fixed `width` or `min-width` in px on a layout element, a grid
    with a fixed column count, a negative margin, or an element set wider than 100%.
  - **Cap the content, not the design.** Give `.container` a `max-width` and centre it, so a
    2560px monitor shows a readable column rather than text stretched across the screen.
    Every full-width band (`.hero`, `.section-alt`, `.cta-band`, `.site-footer`) keeps its
    background edge to edge and puts a `.container` inside for its content — so the colour
    spans the screen and the words do not.

  Prefer rules that need no breakpoint at all: `repeat(auto-fit, minmax(240px, 1fr))` for
  grids, `clamp()` for type and spacing, `%`/`fr`/`ch` over fixed pixels. A layout that
  reflows on its own is right at every width, including the ones nobody tested.

  **A rule with no breakpoint is a desktop rule.** Everything you write outside a media
  query applies at 1920px, so "make it work on mobile and add a max-width query" leaves the
  monitor showing whatever the phone reasoning happened to produce. Three things follow,
  and the build now measures the first two at 1440px and 1920px and fails on them:

  - **A button or badge is as wide as its words, never as wide as its column.** `.btn`,
    `.badge`, `.pill`, `.tag` and `.eyebrow` stay `inline-block`/`inline-flex` with no
    `width: 100%` and no `display: block` outside a mobile media query. A real site shipped
    a hero button 1116px wide: correct at 390px, absurd on a monitor. A full-width submit
    *inside a `<form>`* is fine — the form is already a narrow column.
  - **A band reaches both screen edges.** `.hero`, `.page-hero`, `.section-alt`,
    `.cta-band` and the footer never take a `max-width` themselves; the `.container`
    *inside* them does. The same site capped `.cta-band` at 1180px, so on a 1920 monitor
    its colour floated in the middle while every other band ran edge to edge.
  - **Do not leave half the column empty.** A 1180px container holding one left-aligned
    60ch paragraph is 600px of text and 580px of nothing. Give the wide screen something
    to do: two columns for text beside an image or a list, `auto-fit` grids for repeated
    items, or centre a narrow intro under a centred heading. Judge it at 1920px, not at
    the width the words happen to wrap at.
- On any accent-coloured background (`cta-band`, `btn-primary`, a hero that uses the accent),
  the text colour must be the accent-ink value from the direction above — it is the one that
  is guaranteed readable there.

## The one rule that breaks a page outright

**Never change `position` on a layout container.** `hero`, `page-hero`, `hero-bg`,
`section`, `section-alt`, `container`, `card`, `cta-band`, the footer — these are the
boxes the page is stacked out of, and they must stay in the normal document flow.

`hero-bg` in particular is not a backdrop layer. It **is** the hero `<section>` itself,
carrying its photo as a background image — the markup is
`<section class="hero hero-bg" style="background-image: url(...)">`. Styling it as an
absolutely positioned overlay (`position: absolute; inset: 0`) takes the whole hero out of
the flow, so it reserves no height and the next section renders on top of it. A real site
shipped exactly that and its owner reported overlapping text six times.

Positioned `::before` / `::after` pseudo-elements are the correct way to build an overlay,
and are entirely fine — the darkening layer over `hero-bg` is already one.

## Things that make it look generic — avoid all of them

- Every section identical: same padding, same centred heading, same three-card row.
- Cards that are all white rectangles with the same small radius and the same soft grey shadow.
- The accent used only on buttons. Use it structurally too — a band, a rule, a filled panel,
  an oversized numeral.
- Border-radius everywhere by default. Pick sharp or round deliberately and stay consistent.
- Timid type: a hero heading barely larger than a paragraph.

Write the stylesheet now.
