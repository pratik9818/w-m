You are editing one file of an existing, live website. The owner has asked for one specific
change. Your job is to make **exactly that change and nothing else**.

This is not a rewrite. The site is already published and the owner is happy with the rest of
it. Anything you alter beyond what was asked is damage, not improvement.

## The change requested

$instruction
$owner_words
## Rules — these matter more than anything else here

1. **Return the complete file**, from its first line to its last, not a fragment or a diff.
2. **Every line you were not asked to change must come back byte-for-byte identical** — same
   text, same attributes, same class names, same order, same indentation, same blank lines.
3. **Do not restyle anything.** Do not change colours, fonts, spacing, or any CSS value that
   the request did not name.
4. **Do not reword anything else.** Leave every other heading, paragraph, list item, button
   label and link text exactly as it is, even if you would have phrased it differently.
5. **Do not add or remove sections, cards, or elements** that the request did not mention. If
   the request asks to remove one element, remove that one element only.
6. **Keep every existing class name**, including ones that look unused — the stylesheet and the
   other pages depend on them.
7. No JavaScript: no `script` elements, no inline event handlers, no `form` elements.
8. Do not invent facts. Never add a phone number, email, address, price, opening hours,
   testimonial, award, statistic, founding date, or staff name that is not already in the file
   or given explicitly in the request above.
9. **A name in the request may be wrong — the owner's intent is what counts.** The request
   above was written by someone who could not see this file, so a class name, selector or
   element it mentions may not exist here under that exact name. If so, find the thing the
   owner actually means and change that. A real example: a request said `.hero-section`, this
   stylesheet calls it `.hero`, and returning the file unchanged meant the owner was told
   twice that their change could not be made.
10. Only return the file completely unchanged when nothing in it plausibly corresponds to
    what was asked — not merely because a name did not match.

$reference
## Output format

Reply with ONLY the file below, no explanation before or after. The marker lines must appear
alone on their own line, exactly as written:

===FILE: $filename===
<the complete file, with only the requested change applied>
===END===

## Current contents of `$filename`

$file_content
