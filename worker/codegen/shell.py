"""Builds the parts of every page that are identical by contract.

The head, header/nav and footer are fully determined by the business data and the class
contract -- there is nothing for a model to decide. Having it write them four times per
build cost output tokens for text that must come out the same each time, and any variation
between pages was a defect (an owner asked for exactly that: "Layout, it should consistent
in all pages").

Generating them here makes that consistency structural rather than something we hope for,
and leaves the model to write only what actually needs judgement: the page's own content.
"""
import html

NAV = (
    ("index.html", "Home"),
    ("about.html", "About"),
    ("services.html", "Services"),
    ("contact.html", "Contact"),
)
# A landing page has no other pages to link to, so the menu scrolls to sections on the
# same page. The ids here must match the ones LANDING_REQUIREMENTS tells the model to put
# on those sections.
LANDING_NAV = (
    ("#about", "About"),
    ("#services", "Services"),
    ("#contact", "Contact"),
)


def _nav_for(layout: str | None) -> tuple[tuple[str, str], ...]:
    return LANDING_NAV if layout == "landing" else NAV


def _esc(value: str | None) -> str:
    return html.escape(str(value or ""), quote=True)


def _tel_href(phone: str) -> str:
    """A `tel:` URI cannot contain spaces, but a readable phone number usually does.

    So the href gets the stripped form and the link text keeps the owner's formatting.
    Without this, a perfectly normal "+44 20 1234" produced an href with spaces that
    failed contact_links_valid and would have blocked the build.
    """
    return _esc("".join(ch for ch in phone if not ch.isspace()))


def _header(spec: dict, current: str) -> str:
    name = _esc(spec.get("name"))
    logo_url = spec.get("logo_url")
    logo = f'<img src="{_esc(logo_url)}" alt="{name}" class="logo-mark">' if logo_url else ""
    items = "\n".join(
        f'          <li><a href="{href}" class="nav-link'
        + (' is-current" aria-current="page"' if href == current else '"')
        + f">{label}</a></li>"
        for href, label in _nav_for(spec.get("layout"))
    )
    return f"""  <header class="site-header">
    <div class="container header-inner">
      <a href="index.html" class="logo">{logo}<span class="logo-text">{name}</span></a>
      <nav class="main-nav" aria-label="Primary">
        <ul class="nav-list">
{items}
        </ul>
      </nav>
    </div>
  </header>"""


def _footer(spec: dict) -> str:
    name = _esc(spec.get("name"))
    year = _esc(spec.get("current_year"))
    links = "\n".join(
        f'            <li><a href="{href}" class="nav-link">{label}</a></li>'
        for href, label in _nav_for(spec.get("layout"))
    )
    contact_rows = ""
    if spec.get("phone"):
        contact_rows += (
            f'          <p class="contact-value">'
            f'<a href="tel:{_tel_href(spec["phone"])}">{_esc(spec["phone"])}</a></p>\n'
        )
    if spec.get("email"):
        email = _esc(spec["email"])
        contact_rows += f'          <p class="contact-value"><a href="mailto:{email}">{email}</a></p>\n'

    contact_col = (
        f'        <div class="footer-col">\n'
        f'          <h2 class="footer-title">Get in touch</h2>\n{contact_rows}'
        f'        </div>\n'
    ) if contact_rows else ""

    return f"""  <footer class="site-footer">
    <div class="container footer-inner">
      <div class="footer-col">
        <h2 class="footer-title">{name}</h2>
        <p class="footer-note">&copy; {year} {name}. All rights reserved.</p>
      </div>
      <div class="footer-col">
        <h2 class="footer-title">Pages</h2>
        <ul class="nav-list">
{links}
        </ul>
      </div>
{contact_col}    </div>
  </footer>"""


def render_page(spec: dict, filename: str, title: str, description: str, main_html: str) -> str:
    """Wrap model-written page content in the shared, deterministic document shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_esc(title)}</title>
  <meta name="description" content="{_esc(description)}">
  <link rel="stylesheet" href="style.css">
</head>
<body>
{_header(spec, filename)}
{main_html.strip()}
{_footer(spec)}
</body>
</html>
"""
