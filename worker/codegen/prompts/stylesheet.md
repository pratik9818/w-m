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

The result must look like a professionally designed site, not a default-styled document.

- Build a real design system: CSS custom properties on `:root` for the colour palette, spacing
  scale, radii, and shadows, then use them consistently throughout. Define plenty of them.
- Give the page clear visual hierarchy — a distinct header, a hero with real presence, section
  bands that alternate background via `section-alt`, and generous vertical rhythm between
  sections.
- Style every group in the class contract as a designed component: `card` with padding and a
  subtle shadow, `step-number` with a visually distinct numbered treatment, `faq-item` as a
  styled question block, `cta-band` as a visually distinct full-width band, and `site-footer`
  as a structured multi-column layout.
- Include hover and focus states for `nav-link`, `btn`, `btn-primary`, `btn-secondary`, links,
  and `card`.
- Fully responsive using CSS only — flexbox and grid plus media queries. Include **at least
  three media queries** covering large, tablet, and mobile widths, collapsing multi-column
  layouts to single column on narrow screens. The nav must stay usable on mobile without any
  JavaScript (wrapping or stacking is fine).
- Make sure text has strong contrast against every background you define, including inside
  `hero` and `cta-band`.

Write the stylesheet now.
