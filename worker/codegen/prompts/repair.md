You are fixing specific defects in one file of an existing website. The file is otherwise
finished and correct. Fix only the listed problems and change nothing else.

## Problems found in `$filename`

$problems

## Rules

1. **Return the complete file**, from its first line to its last — not a fragment, not a diff.
2. **Every line not involved in a listed problem must come back byte-for-byte identical** —
   same text, same attributes, same class names, same order, same indentation.
3. Fix only what is listed. Do not restyle, do not reword other content, do not add or
   remove sections, do not "improve" anything you were not asked about.
4. **Never invent information to fill a gap.** If a contact link is empty because the
   business has no phone or email on record, delete that link and its label entirely —
   do not put in a placeholder, an example value, or a made-up address.
5. No JavaScript: no `script` elements, no inline event handlers, no `form` elements.
6. Keep every existing class name, including ones that look unused — the stylesheet and
   the other pages depend on them.

## How to fix the common problems

- *"contact_links_valid: 'mailto:'"* — an anchor whose `href` has nothing after the scheme.
  Remove that entire anchor (and its surrounding `contact-item` if that leaves it empty).
  Never fill it with an invented address.
- *"html_well_formed: `</h>` has no matching opening tag"* — a malformed or mismatched tag.
  Correct it to match the element it actually closes.
- *"no_script_tags"* — delete the `script` element completely. If it was computing
  something like a copyright year, replace it with the literal value.
- *"internal_links_valid"* — the link points at a page that does not exist. Repoint it at
  one of `index.html`, `about.html`, `services.html`, `contact.html`, or remove the link.
- *"content_present: words=N"* — the page is too short. Expand the existing sections with
  more genuine detail about this business. Do not invent facts, prices, dates or reviews.

## Output format

Reply with ONLY the file below, no explanation before or after. The marker lines must
appear alone on their own line, exactly as written:

===FILE: $filename===
<the complete file, with only the listed problems fixed>
===END===

## Current contents of `$filename`

$file_content
