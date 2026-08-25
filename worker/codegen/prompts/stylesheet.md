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
- Fully responsive using CSS only — flexbox and grid plus media queries. Include **at least
  three media queries** covering large, tablet, and mobile widths, collapsing multi-column
  layouts to single column on narrow screens. The nav must stay usable on mobile without any
  JavaScript (wrapping or stacking is fine).
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
