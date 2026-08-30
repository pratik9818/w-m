"""The four policy pages a payment gateway will not activate a merchant without.

Every owner who tries to take money online hits the same wall: Razorpay, Cashfree and PayU
all run automated checks on a merchant's website before activating the account, and all of
them look for the same four documents plus visible contact details. Miss one and the
application is rejected, usually without saying which one.

So this is a real feature rather than a formality. It is also the one part of a site where
a language model must not be anywhere near the output:

  - These pages are read as commitments. A model that improvises "we offer a 30-day
    money-back guarantee" has written a promise the owner never made and may not be able to
    afford, onto a page a payment aggregator has just verified.
  - They must carry the owner's *actual* email, phone and address. Those are facts in the
    database, not things to be inferred.
  - Their whole value is being boringly consistent with what the checkers expect.

Generated from a template with the business's own details filled in, therefore, exactly as
`forms.py` builds a form from a definition. The chat handler refuses to start this at all
until the business has an email, a phone number and an address on file, because pages
missing those fail verification just as surely as pages that do not exist.

The visual shell -- head, header, nav, footer -- is lifted from a page the model already
built for this site, so the policy pages inherit whatever design it chose rather than
looking bolted on. That matters more than it sounds: a reviewer comparing a polished home
page against four pages of naked Times New Roman is looking at something that reads as
fake.

Nothing here is legal advice, and the bot says so in the chat when it publishes them. They
are the standard disclosures, accurately filled in.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape

# The four pages, and the words that go in the footer for each.
POLICY_PAGES: dict[str, str] = {
    "terms.html": "Terms &amp; Conditions",
    "privacy.html": "Privacy Policy",
    "refund.html": "Cancellation &amp; Refunds",
    "shipping.html": "Shipping &amp; Delivery",
}

# Idempotency markers. Injection runs on every build, so both the pages and the footer
# links have to be recognisable as already-there rather than added a second time.
FOOTER_MARKER = "<!-- policy-links -->"
FOOTER_END = "<!-- /policy-links -->"

# A refund timeline has to be stated in days rather than "promptly" -- vague wording is
# itself a rejection reason.
DEFAULT_REFUND_DAYS = 7
MAX_REFUND_DAYS = 30
# What the customer's bank takes after the merchant approves it, which is the number people
# actually complain about when it is missing.
BANK_SETTLEMENT_DAYS = "5-7 business days"

_HEAD_RE = re.compile(r"<head\b[^>]*>(.*?)</head>", re.IGNORECASE | re.DOTALL)
_HEADER_RE = re.compile(r"<header\b.*?</header>", re.IGNORECASE | re.DOTALL)
_FOOTER_RE = re.compile(r"<footer\b.*?</footer>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title\b[^>]*>.*?</title>", re.IGNORECASE | re.DOTALL)
_IS_CURRENT_RE = re.compile(r"\s*\bis-current\b")
_BLOCK_RE = re.compile(re.escape(FOOTER_MARKER) + r".*?" + re.escape(FOOTER_END), re.DOTALL)


def _clean(value) -> str:
    return str(value).strip() if value and str(value).strip() else ""


def missing_details(details: dict) -> list[str]:
    """Which of the three facts a verifier insists on are not on file yet.

    Phrased the way the owner would say them, because this list is read out in the chat. A
    policy page saying "contact us" with no way to do so is exactly what gets an
    application rejected, so it is better to ask for a phone number once than to publish
    four pages that fail.
    """
    wanted = (
        ("email", "an email address"),
        ("phone", "a phone number"),
        ("address", "a business address"),
    )
    return [label for key, label in wanted if not _clean(details.get(key))]


def _shell(files: dict[str, str]) -> tuple[str, str, str]:
    """The head, header and footer of a page the model already wrote for this site.

    Taken verbatim so the policy pages carry the same fonts, nav, logo and CDN assets as
    everything else. Falling back to something plain rather than raising is deliberate:
    pages that look a little bare still pass verification, and pages that do not exist do
    not.
    """
    source = ""
    for name in ("index.html", "about.html", "contact.html", "services.html"):
        if files.get(name):
            source = files[name]
            break

    head_match = _HEAD_RE.search(source)
    head = head_match.group(1) if head_match else '<link rel="stylesheet" href="style.css">'
    head = _TITLE_RE.sub("", head).strip()

    header_match = _HEADER_RE.search(source)
    # The copied nav still marks whichever page it came from as the current one, which
    # would light up "About" while somebody reads the refund policy.
    header = _IS_CURRENT_RE.sub("", header_match.group(0)) if header_match else ""

    footer_match = _FOOTER_RE.search(source)
    footer = footer_match.group(0) if footer_match else ""
    return head, header, footer


def _contact_block(details: dict) -> str:
    rows = []
    for key, label in (("email", "Email"), ("phone", "Phone"), ("address", "Address")):
        value = _clean(details.get(key))
        if not value:
            continue
        if key == "email":
            shown = f'<a href="mailto:{escape(value)}">{escape(value)}</a>'
        elif key == "phone":
            shown = f'<a href="tel:{escape(value.replace(" ", ""))}">{escape(value)}</a>'
        else:
            shown = escape(value)
        rows.append(
            f'<div class="contact-item"><span class="contact-label">{label}</span>'
            f'<span class="contact-value">{shown}</span></div>'
        )
    if not rows:
        return ""
    return '<div class="contact-list">' + "".join(rows) + "</div>"


def _para(text: str) -> str:
    return f"<p>{text}</p>"


def _terms(name: str, details: dict) -> str:
    return "".join([
        _para(f"These terms govern your use of this website and anything you buy from "
              f"{name}. By placing an order you accept them."),
        "<h2>What we provide</h2>",
        _para("What is included in each plan or service is described on this website, and "
              "is what you are buying."),
        "<h2>Prices and payment</h2>",
        _para("Prices shown on this website are in Indian Rupees and include applicable "
              "taxes unless stated otherwise. Payment is collected through our payment "
              "provider. We never see or store your card or UPI details."),
        _para("Where a plan is billed on a recurring basis, it renews automatically at the "
              "interval shown when you bought it, until you cancel."),
        "<h2>Your responsibilities</h2>",
        _para("You agree to give accurate information, to use the service lawfully, and not "
              "to use it to publish anything unlawful, misleading, or that infringes "
              "somebody else's rights. We may suspend an account that does."),
        "<h2>Availability</h2>",
        _para("We work to keep the service available and correct, but we do not guarantee "
              "it will be uninterrupted or error-free. We are not liable for indirect or "
              "consequential loss. Nothing here limits liability that cannot be limited by "
              "law."),
        "<h2>Cancellation</h2>",
        _para("You can cancel at any time. See our "
              '<a href="refund.html">Cancellation and Refunds policy</a> for how that works '
              "and what is refundable."),
        "<h2>Changes</h2>",
        _para("We may update these terms. The date at the top of this page is when they "
              "last changed, and the current version is always the one published here."),
        "<h2>Governing law</h2>",
        _para("These terms are governed by the laws of India, and the courts of India have "
              "jurisdiction over any dispute."),
        "<h2>Contact</h2>",
        _para("If anything here is unclear, please get in touch:"),
        _contact_block(details),
    ])


def _privacy(name: str, details: dict) -> str:
    return "".join([
        _para(f"This policy explains what personal information {name} collects, why, and "
              "what we do with it."),
        "<h2>What we collect</h2>",
        _para("We collect what you give us: your name, email address and phone number when "
              "you contact us or place an order, and the details of what you bought. If you "
              "send a message through a form on this site, we collect what you wrote in it."),
        _para("We may also collect basic technical information, such as which pages are "
              "visited, in aggregate, to understand what is useful."),
        "<h2>Why we collect it</h2>",
        _para("To provide what you have asked for, to take payment, to reply to you, to keep "
              "records we are required to keep, and to tell you about something important to "
              "your account or order."),
        "<h2>Payment information</h2>",
        _para("Payments are processed by our payment provider. Your card, UPI and bank "
              "details are handled by them under their own security standards and are never "
              "stored on our systems."),
        "<h2>Who we share it with</h2>",
        _para("We do not sell your personal information. We share it only with the providers "
              "who make the service work -- our payment provider, and our hosting and "
              "messaging providers -- and only what they need. We may disclose information "
              "where the law requires it."),
        "<h2>How long we keep it</h2>",
        _para("For as long as you are a customer, and afterwards only for as long as we need "
              "it for legal, accounting or dispute-resolution purposes."),
        "<h2>Your rights</h2>",
        _para("You can ask for a copy of the information we hold about you, ask us to "
              "correct it, or ask us to delete it where we are not required to keep it. "
              "Write to us using the details below and we will respond."),
        "<h2>Contact</h2>",
        _para("For any question about your information, or to make a request:"),
        _contact_block(details),
    ])


def _refund(name: str, details: dict, refund_days: int) -> str:
    return "".join([
        _para(f"This policy explains how to cancel something you have bought from {name}, "
              "and when you get your money back."),
        "<h2>Cancelling</h2>",
        _para("You can cancel at any time, through the same channel you bought through or by "
              "contacting us with the details below. A recurring plan stops at the end of "
              "the period you have already paid for: you keep what you have paid for until "
              "then, and are not charged again."),
        "<h2>Refunds</h2>",
        _para(f"If you are not happy with what you have bought, tell us within "
              f"<strong>{refund_days} days</strong> of payment and we will refund it in "
              "full."),
        _para("After that period, a payment already taken for a completed period is not "
              "refundable, because the service for that period has been provided. "
              "Cancelling stops any future payment."),
        _para("If you were charged in error, or charged twice, tell us and we will refund it "
              "regardless of when it happened."),
        "<h2>How long a refund takes</h2>",
        _para("An approved refund is issued to the original payment method. Once we have "
              f"approved it, the money reaches your account within "
              f"<strong>{BANK_SETTLEMENT_DAYS}</strong>, depending on your bank or UPI "
              "provider. We cannot make that step faster, but we will tell you the date we "
              "sent it."),
        "<h2>How to ask</h2>",
        _para("Contact us with the email address or phone number you used to pay, and the "
              "date of the payment:"),
        _contact_block(details),
    ])


def _shipping(name: str, details: dict) -> str:
    """Required even though nothing is shipped.

    The check looks for the page, so saying plainly that this is a digital service is what
    passes it. Leaving it out because "we do not ship anything" is a common rejection.
    """
    return "".join([
        _para(f"{name} provides a digital service. Nothing physical is shipped, so there are "
              "no delivery charges and no courier involved."),
        "<h2>How and when you get what you bought</h2>",
        _para("Access is delivered electronically. In the normal case it is available "
              "immediately once your payment is confirmed, and never later than 24 hours "
              "after it. We will tell you when it is ready, using the email address or phone "
              "number you gave us."),
        "<h2>If something has not arrived</h2>",
        _para("If your payment has gone through and you have not received what you bought, "
              "contact us and we will sort it out. Please have the date and amount of the "
              "payment to hand."),
        "<h2>Contact</h2>",
        _contact_block(details),
    ])


def _page(
    title_html: str, body: str, head: str, header: str, footer: str,
    updated: str, business_name: str,
) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        f"<title>{title_html} | {escape(business_name)}</title>\n"
        f"{head}\n</head>\n<body>\n{header}\n<main>\n"
        '<section class="page-hero"><div class="container hero-inner">'
        f'<h1 class="hero-title">{title_html}</h1>'
        f'<p class="hero-subtitle">Last updated {escape(updated)}</p>'
        "</div></section>\n"
        '<section class="section"><div class="container legal-copy">\n'
        f"{body}\n"
        "</div></section>\n"
        f"</main>\n{footer}\n</body>\n</html>\n"
    )


def _footer_links(current: str = "") -> str:
    links = []
    for name, label in POLICY_PAGES.items():
        if name == current:
            links.append(f'<span class="nav-link is-current">{label}</span>')
        else:
            links.append(f'<a class="nav-link" href="{name}">{label}</a>')
    return (
        FOOTER_MARKER
        + '<div class="footer-col footer-policies"><p class="footer-note">'
        + " &middot; ".join(links)
        + "</p></div>"
        + FOOTER_END
    )


def _inject_footer_links(page: str, current: str) -> str:
    """Put the row of policy links inside the page's footer, exactly once.

    A verifier looks for the documents to be reachable from the site, not merely to exist
    at a URL somebody typed in, so this is part of passing rather than decoration.
    """
    block = _footer_links(current)
    if FOOTER_MARKER in page:
        return _BLOCK_RE.sub(lambda _m: block, page, count=1)

    match = _FOOTER_RE.search(page)
    if not match:
        # No footer to put them in. Appending before </body> keeps them reachable, which is
        # the thing that actually matters.
        block_html = f'<footer class="site-footer">{block}</footer>'
        if "</body>" in page:
            return page.replace("</body>", block_html + "</body>", 1)
        return page + block_html

    footer_html = match.group(0)
    closing = footer_html.rfind("</footer>")
    updated = footer_html[:closing] + block + footer_html[closing:]
    return page[:match.start()] + updated + page[match.end():]


def _strip_footer_links(page: str) -> str:
    return _BLOCK_RE.sub("", page)


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def apply_policies(
    files: dict[str, str], policies: dict | None, details: dict
) -> dict[str, str]:
    """Put the site's policy pages onto it, or take them off again.

    Runs on every build, like `apply_forms`, and for the same reason: a rebuild rewrites
    every page from scratch, and pages that existed only as files from a previous build
    would disappear -- taking the owner's payment gateway approval with them.
    """
    out = dict(files)
    enabled = bool((policies or {}).get("enabled"))

    if not enabled:
        for name in POLICY_PAGES:
            out.pop(name, None)
        for name in list(out):
            if name.endswith(".html"):
                out[name] = _strip_footer_links(out[name])
        return out

    settings = policies or {}
    refund_days = settings.get("refund_days") or DEFAULT_REFUND_DAYS
    try:
        refund_days = max(1, min(int(refund_days), MAX_REFUND_DAYS))
    except (TypeError, ValueError):
        refund_days = DEFAULT_REFUND_DAYS

    business_name = (
        _clean(settings.get("legal_name")) or _clean(details.get("name")) or "This business"
    )
    updated = _clean(settings.get("updated_on")) or today_iso()
    head, header, footer = _shell(files)

    escaped_name = escape(business_name)
    bodies = {
        "terms.html": _terms(escaped_name, details),
        "privacy.html": _privacy(escaped_name, details),
        "refund.html": _refund(escaped_name, details, refund_days),
        "shipping.html": _shipping(escaped_name, details),
    }
    for name, title_html in POLICY_PAGES.items():
        out[name] = _page(
            title_html, bodies[name], head, header, footer, updated, business_name
        )

    for name in list(out):
        if name.endswith(".html"):
            out[name] = _inject_footer_links(out[name], current=name)

    return out
