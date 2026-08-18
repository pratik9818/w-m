$shared

## Your task in this response: $page_names

Write only the pages listed below. The other pages and the stylesheet are generated separately
— do not write them, and do not write any CSS. Assume `style.css` already exists and styles
every class in the contract above.

Reply with ONLY the files below, no explanation before, during, or after. Each marker line must
appear alone on its own line, exactly as written:

$output_format

## Rules for every page you write

- A complete, valid HTML document: `<!DOCTYPE html>`, `<html lang="en">`, a `<head>` with its
  own unique `<title>` and `<meta name="description">` written from this business's real
  details, a viewport meta tag, `<link rel="stylesheet" href="style.css">`, then `<body>`.
  Never a fragment.
- The same header on every page: the business name (and logo image if the data gives a logo
  URL) plus a nav linking to `index.html`, `about.html`, `services.html` and `contact.html`.
  Mark the current page's link with the `is-current` class.
- The same footer on every page, using the literal copyright year from the business data.
- Only link to those four page files. Never link to a page or an anchor that does not exist.
- **At least 400 words of real body copy per page.** Short pages are a failure.
- Use the shared class contract for every structure. Wrap section content in `container`.

## Required content

$page_requirements

Write the pages now.
