$shared

## Your task in this response: $page_names

Write only the pages listed below. The other pages and the stylesheet are generated separately
— do not write them, and do not write any CSS. Assume `style.css` already exists and styles
every class in the contract above.

Reply with ONLY the files below, no explanation before, during, or after. Each marker line must
appear alone on its own line, exactly as written:

$output_format

Where each `<the …>` placeholder is the title, meta description and `<main>` block described
below — not a whole HTML document.

## What to write for each page

**Do not write the page shell.** The `<!DOCTYPE>`, `<head>`, the site header with its
navigation, and the footer are added automatically and are identical on every page — writing
them yourself would be discarded. Write only the three pieces below, in this order:

1. `<title>` — unique to this page, written from the business's real details
2. `<meta name="description" content="...">` — unique to this page
3. `<main>` … `</main>` — all of the page's own content

So each file looks exactly like this, and nothing else:

```html
<title>Page title here</title>
<meta name="description" content="Description here">
<main>
  <section class="page-hero">…</section>
  <section class="section">…</section>
</main>
```

- **At least 400 words of real body copy inside `<main>`.** Short pages are a failure.
- Use the shared class contract for every structure. Wrap section content in `container`.
- Only ever link to `index.html`, `about.html`, `services.html` or `contact.html`. Never link
  to a page or an anchor that does not exist.

## Required content

$page_requirements

Write the pages now.
