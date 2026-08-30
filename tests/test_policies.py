"""The pages that decide whether an owner can take money at all.

These are not decoration. A payment aggregator runs an automated check over the merchant's
website and refuses to activate the account if the four documents are missing, or if they
do not carry a reachable email, phone number and address. So the failures worth guarding
against here are the quiet ones:

  - a **rebuild silently dropping them**, which is how an owner loses an approval they
    already have and finds out from a rejection email weeks later;
  - **contact details missing** from a page that otherwise looks complete, which fails
    verification for a reason nobody reading the page would guess;
  - **duplication**, because injection runs on every build and a second copy of the footer
    links is the kind of thing that never gets noticed until a customer mentions it.
"""
import pytest

from bot_api.services.plans import weight_for_operation
from worker.codegen.policies import (
    POLICY_PAGES,
    apply_policies,
    missing_details,
)
from worker.codegen.validate import failed as failed_checks
from worker.codegen.validate import validate_files

DETAILS = {
    "name": "Sharma Sweets",
    "email": "hello@sharmasweets.in",
    "phone": "98765 43210",
    "address": "12 MG Road, Indore 452001",
}

ENABLED = {"enabled": True, "refund_days": 7, "updated_on": "2026-08-30"}


def _site() -> dict[str, str]:
    """A stand-in for what the model produces: four pages sharing a header and footer."""
    def page(title: str, current: str) -> str:
        nav = "".join(
            f'<a class="nav-link{" is-current" if f == current else ""}" href="{f}">{f}</a>'
            for f in ("index.html", "about.html", "services.html", "contact.html")
        )
        return (
            '<!doctype html><html lang="en"><head>'
            f"<title>{title}</title>"
            '<link rel="stylesheet" href="style.css">'
            "</head><body>"
            f'<header class="site-header"><div class="header-inner">'
            f'<span class="logo-text">Sharma Sweets</span>'
            f'<nav class="main-nav"><div class="nav-list">{nav}</div></nav></div></header>'
            f"<main><h1>{title}</h1></main>"
            '<footer class="site-footer"><div class="footer-inner">'
            '<p class="footer-note">© Sharma Sweets</p></div></footer>'
            "</body></html>"
        )

    return {
        "index.html": page("Home", "index.html"),
        "about.html": page("About", "about.html"),
        "services.html": page("Services", "services.html"),
        "contact.html": page("Contact", "contact.html"),
        "style.css": ".site-header { color: #222; }",
    }


# ------------------------------------------------------------------ the four pages


def test_all_four_pages_are_created():
    """Four, not three. Shipping is the one people leave out because nothing is shipped,
    and it is a rejection every time."""
    out = apply_policies(_site(), ENABLED, DETAILS)
    for name in ("terms.html", "privacy.html", "refund.html", "shipping.html"):
        assert name in out, f"{name} was not created"


def test_every_policy_page_carries_the_contact_details():
    """The check looks for a reachable email, phone and address. A page without them looks
    finished and fails anyway."""
    out = apply_policies(_site(), ENABLED, DETAILS)
    for name in POLICY_PAGES:
        page = out[name]
        assert DETAILS["email"] in page, f"{name} has no email"
        assert DETAILS["phone"] in page, f"{name} has no phone"
        assert DETAILS["address"] in page, f"{name} has no address"
        assert "mailto:" in page and "tel:" in page


def test_the_shipping_page_says_plainly_that_nothing_is_shipped():
    out = apply_policies(_site(), ENABLED, DETAILS)
    shipping = out["shipping.html"].lower()
    assert "digital service" in shipping
    assert "nothing physical is shipped" in shipping


def test_the_refund_page_states_a_number_of_days_not_a_vague_promise():
    """"We will refund promptly" is itself a rejection reason."""
    out = apply_policies(_site(), {"enabled": True, "refund_days": 14}, DETAILS)
    assert "14 days" in out["refund.html"]
    # And the bank's own leg, which is what customers actually chase.
    assert "5-7 business days" in out["refund.html"]


@pytest.mark.parametrize("given,expected", [
    (99, "30 days"), (None, "7 days"), ("banana", "7 days"), (10, "10 days"),
    # Zero reads as "the owner did not give a number", not as "no refunds ever" -- those
    # need different wording on the page, not a nought in a sentence about days.
    (0, "7 days"),
])
def test_the_refund_window_is_bounded(given, expected):
    """A model reading "refund whenever" must not be able to write an unbounded promise
    onto a page the owner is then held to."""
    out = apply_policies(_site(), {"enabled": True, "refund_days": given}, DETAILS)
    assert expected in out["refund.html"]


# ------------------------------------------------------------------ linking


def test_the_pages_are_linked_from_every_page_of_the_site():
    """Existing at a URL is not enough -- they have to be reachable from the site."""
    out = apply_policies(_site(), ENABLED, DETAILS)
    for name in [n for n in out if n.endswith(".html")]:
        for target in POLICY_PAGES:
            if target == name:
                continue
            assert f'href="{target}"' in out[name], f"{name} does not link to {target}"


def test_running_twice_does_not_duplicate_the_links():
    """Injection runs on every build, so this is the normal case rather than an edge one."""
    once = apply_policies(_site(), ENABLED, DETAILS)
    twice = apply_policies(once, ENABLED, DETAILS)
    assert twice["index.html"].count("<!-- policy-links -->") == 1
    assert twice["index.html"].count('href="terms.html"') == 1


def test_a_page_does_not_link_to_itself():
    out = apply_policies(_site(), ENABLED, DETAILS)
    assert 'href="terms.html"' not in out["terms.html"]
    assert "is-current" in out["terms.html"]


# ------------------------------------------------------------------ surviving a rebuild


def test_a_rebuild_that_rewrites_every_page_puts_them_back():
    """The failure this whole design exists to prevent.

    A redesign regenerates the four pages from scratch. If the policy pages lived only as
    files from an earlier build, they would vanish here -- and the owner would lose a
    payment gateway approval without a single message telling them so.
    """
    rebuilt = _site()  # fresh from the model: no policy pages, no links
    assert "terms.html" not in rebuilt

    out = apply_policies(rebuilt, ENABLED, DETAILS)
    assert set(POLICY_PAGES) <= set(out)
    assert 'href="privacy.html"' in out["index.html"]


def test_the_pages_inherit_the_sites_own_design():
    """Four pages of unstyled text next to a polished home page reads as fake to a
    reviewer, so the shell is taken from a page the model actually built."""
    out = apply_policies(_site(), ENABLED, DETAILS)
    terms = out["terms.html"]
    assert '<link rel="stylesheet" href="style.css">' in terms
    assert 'class="site-header"' in terms
    assert "Sharma Sweets" in terms


def test_the_copied_nav_does_not_claim_another_page_is_current():
    out = apply_policies(_site(), ENABLED, DETAILS)
    header = out["terms.html"].split("<main>")[0]
    # The home page's nav link was marked current in the source; it must not still be,
    # or "Home" lights up while somebody reads the refund policy.
    assert 'href="index.html">' in header
    assert "is-current" not in header


def test_a_site_with_no_footer_still_gets_the_links():
    files = {"index.html": "<html><body><main>hi</main></body></html>"}
    out = apply_policies(files, ENABLED, DETAILS)
    assert 'href="terms.html"' in out["index.html"]


# ------------------------------------------------------------------ removal


def test_removal_takes_the_pages_and_the_links_away():
    out = apply_policies(_site(), ENABLED, DETAILS)
    back = apply_policies(out, {}, DETAILS)
    for name in POLICY_PAGES:
        assert name not in back
    assert "policy-links" not in back["index.html"]
    assert 'href="terms.html"' not in back["index.html"]


def test_nothing_happens_to_a_site_that_never_had_them():
    original = _site()
    out = apply_policies(original, None, DETAILS)
    assert out == original


# ------------------------------------------------------------------ safety


def test_missing_details_names_what_is_needed_in_the_owners_words():
    assert missing_details(DETAILS) == []
    assert missing_details({"name": "X"}) == [
        "an email address", "a phone number", "a business address",
    ]
    assert missing_details({**DETAILS, "phone": "   "}) == ["a phone number"]


def test_a_business_name_cannot_inject_markup():
    """The name is the owner's text and lands in a <title> and a heading."""
    hostile = '<script>alert(1)</script>'
    out = apply_policies(
        _site(), {"enabled": True, "legal_name": hostile}, {**DETAILS, "name": hostile}
    )
    assert "<script>alert(1)</script>" not in out["terms.html"]
    assert "&lt;script&gt;" in out["terms.html"]


def test_the_generated_pages_are_valid_enough_to_ship():
    """They go through the same pre-flight as everything else, so a malformed one would
    fail a build rather than only looking wrong.

    Only failures naming a policy page count. The stand-in site above is four sentences
    long and trips the "is there any content here" check on its own, which says nothing
    about the pages under test.
    """
    out = apply_policies(_site(), ENABLED, DETAILS)
    problems = [
        detail
        for check in failed_checks(validate_files(out))
        for detail in check["detail"]
        if any(name in str(detail) for name in POLICY_PAGES)
    ]
    assert not problems, problems


def test_adding_policies_costs_what_a_structural_change_costs():
    assert weight_for_operation("add_policies") == 2
    assert weight_for_operation("remove_policies") == 1


# ------------------------------------------------------------------ the details loop


def _business():
    from types import SimpleNamespace
    return SimpleNamespace(name="Sharma Sweets", email=None, phone=None, address=None)


def test_contact_details_sent_with_the_request_are_stored():
    """The loop this fixes, from a real transcript.

    The bot asked for a missing address; the owner sent one; the parser read that answer
    as another request for policy pages, because that is what the conversation was about;
    the address went nowhere and the same question came back. Twice, before the owner
    gave up and started talking about paid plans instead.
    """
    from bot_api.bot.handlers.edit import _store_supplied_contact

    business = _business()
    stored = _store_supplied_contact(business, {
        "operation": "add_policies",
        "email": "hello@sharmasweets.in",
        "phone": "98765 43210",
        "address": "12 MG Road, Indore 452001",
    })
    assert sorted(stored) == ["address", "email", "phone"]
    assert business.address == "12 MG Road, Indore 452001"
    assert missing_details({
        "email": business.email, "phone": business.phone, "address": business.address,
    }) == []


def test_a_malformed_email_is_not_stored():
    """It would be printed on four pages a payment provider then checks."""
    from bot_api.bot.handlers.edit import _store_supplied_contact

    business = _business()
    assert _store_supplied_contact(business, {"email": "hello@"}) == []
    assert business.email is None


def test_a_request_with_no_details_changes_nothing():
    from bot_api.bot.handlers.edit import _store_supplied_contact

    business = _business()
    business.address = "already here"
    assert _store_supplied_contact(business, {"operation": "add_policies"}) == []
    assert business.address == "already here"
