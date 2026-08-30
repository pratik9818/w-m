"""The failures here cost money in one direction or the other.

Billing has two ways to go wrong and they are not symmetrical. Granting something nobody
paid for is a slow leak. Charging somebody who did not get what they paid for, or taking a
month's money twice, is the kind of mistake a small business tells other small businesses
about. So the cases below concentrate on the three places where either could happen:

  - the **signature checks**, which are the only thing standing between "Razorpay said so"
    and "anyone with the URL said so", and whose two constructions are easy to swap;
  - **idempotency**, because Razorpay delivers a webhook more than once and the second
    delivery of `subscription.charged` must not grant a second month;
  - the **weighting**, because "colour changes are always free" is a promise made on the
    pricing page and it is one line of code away from being false.
"""
import asyncio
import hashlib
import hmac
import json
import re
from pathlib import Path

import pytest

from bot_api.config import get_settings
from bot_api.services.billing import (
    owner_from_notes,
    unix_to_datetime,
    verify_checkout_signature,
    verify_webhook_signature,
)
from bot_api.services.billing_events import HANDLED
from bot_api.services.plans import (
    BUSINESS,
    FREE,
    PAID_PLANS,
    PLANS,
    STARTER,
    price_paise,
    price_rupees,
    weight_for_operation,
)
from bot_api.web.pages import render_checkout, render_result
from bot_api.web.routes import _fallback_event_id

WEBHOOK_SECRET = "whsec-for-tests"
KEY_SECRET = "keysec-for-tests"


@pytest.fixture
def secrets(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_webhook_secret", WEBHOOK_SECRET, raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", KEY_SECRET, raising=False)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key", raising=False)
    return settings


# --------------------------------------------------------------- prices


def test_prices_are_what_the_pricing_page_says():
    assert STARTER.monthly_rupees == 999
    assert BUSINESS.monthly_rupees == 1999
    # Two months free, which is the only reason to pay a year up front.
    assert STARTER.yearly_rupees == STARTER.monthly_rupees * 10
    assert BUSINESS.yearly_rupees == BUSINESS.monthly_rupees * 10


def test_every_plan_survives_a_customer_who_uses_all_of_it():
    """The design rule, asserted rather than trusted.

    ₹17 is the measured all-in cost of one realised change and ₹18 of one new site, both
    from `token_usage`. Razorpay keeps about 2.36% (2% plus GST on the fee). If a plan
    ever fails this, somebody has raised an allowance without re-checking the arithmetic.
    """
    cost_per_change = 17
    cost_per_site = 18
    for plan in PAID_PLANS:
        received = price_rupees(plan, "monthly") * (1 - 0.0236)
        worst_case = plan.changes * cost_per_change + plan.sites * cost_per_site
        assert worst_case < received, (
            f"{plan.name} loses money at full use: ₹{worst_case} cost vs ₹{received:.0f} received"
        )


def test_free_plan_is_a_bounded_acquisition_cost():
    """What a stranger can cost before they have paid anything.

    `sites` is temporarily 25 for development (see FREE_SITES_WHILE_TESTING) rather than
    the 1 it is priced at, so the assertion here is deliberately the loose one: the free
    plan must stay free, must stay non-recurring, and must keep its five-change lifetime
    cap, which is the ceiling that actually bounds the spend. Restoring sites to 1 should
    tighten this back up.
    """
    assert not FREE.recurring
    assert FREE.monthly_paise == 0
    # A lifetime allowance, not a monthly one -- this is the property that makes the free
    # plan a one-off cost per stranger rather than a standing one, and it holds whatever
    # the numbers are set to.
    assert FREE.changes > 0
    # The token ceiling must stay ahead of the change count, or it silently becomes the
    # real limit and refuses changes the owner was told they had.
    assert FREE.token_ceiling >= FREE.changes * 60_000


def test_price_lookup_matches_the_plan_objects():
    for plan in PLANS.values():
        assert price_paise(plan, "monthly") == plan.monthly_paise
        assert price_paise(plan, "yearly") == plan.yearly_paise
        # Anything that is not "yearly" is a month. A typo in a callback payload must not
        # silently sell a year for the price of a month.
        assert price_paise(plan, "nonsense") == plan.monthly_paise


# --------------------------------------------------------------- weights


def test_style_changes_are_free():
    """The one promise made in plain words on the pricing page and in the bot's copy."""
    assert weight_for_operation("set_style") == 0
    assert weight_for_operation("set_theme") == 0


def test_questions_and_clarifications_cost_nothing():
    assert weight_for_operation("not_an_edit") == 0
    assert weight_for_operation("clarify") == 0


def test_a_rebuild_costs_more_than_a_text_edit():
    assert weight_for_operation("rebuild_site") == 5
    assert weight_for_operation("change_layout") == 5
    assert weight_for_operation("add_form") == 2
    assert weight_for_operation("patch_site") == 1


def test_an_unknown_operation_costs_one_not_five():
    """A new operation added later must not silently bill somebody five changes."""
    assert weight_for_operation("some_operation_added_next_month") == 1


# --------------------------------------------------------------- signatures


def test_webhook_signature_accepts_what_razorpay_would_send(secrets):
    body = json.dumps({"event": "subscription.charged"}).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, signature) is True


def test_webhook_signature_rejects_a_tampered_body(secrets):
    body = json.dumps({"event": "subscription.charged"}).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body + b" ", signature) is False


def test_webhook_signature_rejects_a_missing_one(secrets):
    assert verify_webhook_signature(b"{}", None) is False
    assert verify_webhook_signature(b"{}", "") is False


def test_webhook_signature_refuses_everything_when_unconfigured(monkeypatch):
    """An unset secret must fail closed, not wave everything through."""
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", "", raising=False)
    body = b"{}"
    assert verify_webhook_signature(body, hashlib.sha256(body).hexdigest()) is False


def test_checkout_signature_uses_payment_then_subscription(secrets):
    """The order is the trap: for an order it is the other way round.

    Signing `subscription|payment` instead of `payment|subscription` produces a check that
    rejects every genuine payment, which looks like an outage rather than a bug.
    """
    payment_id, subscription_id = "pay_ABC", "sub_XYZ"
    correct = hmac.new(
        KEY_SECRET.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256
    ).hexdigest()
    reversed_order = hmac.new(
        KEY_SECRET.encode(), f"{subscription_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()

    assert verify_checkout_signature(payment_id, subscription_id, correct) is True
    assert verify_checkout_signature(payment_id, subscription_id, reversed_order) is False


# --------------------------------------------------------------- webhook plumbing


def test_owner_is_read_back_out_of_the_notes():
    """`notes` is how a renewal fourteen months from now knows whose account to credit."""
    assert owner_from_notes({"notes": {"telegram_id": "12345"}}) == 12345
    assert owner_from_notes({"notes": {}}) is None
    assert owner_from_notes({}) is None
    assert owner_from_notes({"notes": {"telegram_id": ""}}) is None
    # Garbage must not raise in a webhook handler; it must be reported as unattributable.
    assert owner_from_notes({"notes": {"telegram_id": "not-a-number"}}) is None


def test_unix_timestamps_become_dates_and_rubbish_becomes_none():
    assert unix_to_datetime(1_756_000_000).year == 2025
    assert unix_to_datetime(None) is None
    assert unix_to_datetime("") is None
    assert unix_to_datetime("banana") is None


def test_fallback_event_id_is_stable_across_retries():
    """Two deliveries of one charge share an id; next month's charge does not.

    Without this, a webhook arriving with no event-id header would look unique on every
    retry and grant a month each time.
    """
    body = {
        "created_at": 1_756_000_000,
        "payload": {
            "subscription": {"entity": {"id": "sub_1", "current_start": 1_756_000_000}},
            "payment": {"entity": {"id": "pay_1"}},
        },
    }
    first = _fallback_event_id("subscription.charged", body)
    assert first == _fallback_event_id("subscription.charged", body)

    next_month = json.loads(json.dumps(body))
    next_month["payload"]["payment"]["entity"]["id"] = "pay_2"
    assert _fallback_event_id("subscription.charged", next_month) != first


def test_fallback_event_id_fits_the_column():
    body = {"payload": {"subscription": {"entity": {"id": "s" * 200}},
                        "payment": {"entity": {"id": "p" * 200}}}}
    assert len(_fallback_event_id("subscription.charged", body)) <= 80


def test_the_events_we_act_on_are_the_ones_worth_subscribing_to():
    """Kept in step with the webhook subscription list documented in .env.example."""
    assert HANDLED == {
        "subscription.activated",
        "subscription.charged",
        "subscription.pending",
        "subscription.halted",
        "subscription.cancelled",
        "subscription.completed",
    }


# --------------------------------------------------------------- the payment page


def test_checkout_page_shows_the_price_on_the_button():
    """Nobody should tap a payment button wondering what they are about to be charged."""
    html = render_checkout(
        plan=STARTER, period="monthly", amount_paise=STARTER.monthly_paise,
        subscription_id="sub_A", razorpay_key_id="rzp_test_key", token="tok",
    )
    assert "Pay ₹999" in html
    assert "₹999" in html


def test_checkout_page_never_carries_the_api_secret(secrets):
    html = render_checkout(
        plan=BUSINESS, period="yearly", amount_paise=BUSINESS.yearly_paise,
        subscription_id="sub_B", razorpay_key_id="rzp_test_key", token="tok",
    )
    assert KEY_SECRET not in html
    assert "rzp_test_key" in html


def test_checkout_page_reads_its_config_from_json_not_interpolated_js():
    """The JavaScript must contain no server-substituted values.

    Interpolating into the script is how the page ends up rendering perfectly and doing
    nothing when clicked, because Python's braces and JavaScript's collided.
    """
    html = render_checkout(
        plan=STARTER, period="monthly", amount_paise=STARTER.monthly_paise,
        subscription_id="sub_C", razorpay_key_id="rzp_test_key", token="tok-9",
    )
    assert '<script id="checkout-config" type="application/json">' in html
    config_start = html.index('id="checkout-config"')
    config_end = html.index("</script>", config_start)
    config = json.loads(html[html.index(">", config_start) + 1:config_end])
    assert config["subscriptionId"] == "sub_C"
    assert config["confirmUrl"] == "/pay/tok-9/confirm"
    assert config["doneUrl"] == "/pay/tok-9/done"


def test_a_hostile_business_name_cannot_escape_either_context():
    """Business names are owner-supplied, so they land in the page as attacker-controlled text.

    They reach two places and each has its own escaping rule, which is exactly the sort of
    pair that gets half-fixed:

      - inside the JSON config block, where the danger is not HTML at all but the literal
        `</script>` ending the block early and dumping the rest into the document;
      - inside the visible "This is for ..." line, where ordinary HTML escaping applies.
    """
    hostile = '</script><img src=x onerror=alert(1)>'
    html = render_checkout(
        plan=STARTER, period="monthly", amount_paise=STARTER.monthly_paise,
        subscription_id="sub_D", razorpay_key_id="k", token="tok",
        business_name=hostile,
    )

    # The block must end where it is supposed to, and still parse -- which is only true
    # if the `</` inside the name was escaped.
    start = html.index('id="checkout-config"')
    body_start = html.index(">", start) + 1
    body_end = html.index("</script>", body_start)
    assert json.loads(html[body_start:body_end])["name"] == hostile

    # The visible line is HTML, so it gets HTML escaping. The tag must never appear live.
    assert "<img src=x" not in html[body_end:]
    assert "&lt;/script&gt;&lt;img src=x" in html


def test_expired_link_page_offers_a_way_back():
    html = render_result(
        ok=False, headline="This payment link has expired",
        detail="Send /upgrade for a fresh one.", bot_username="teko21bot",
    )
    assert "https://t.me/teko21bot" in html
    assert "expired" in html.lower()


def test_result_page_works_without_a_bot_username():
    html = render_result(ok=True, headline="Payment received", detail="Done.")
    assert "t.me" not in html


# --------------------------------------------------- the standalone site in web/


def _call(coro):
    return asyncio.run(coro)


def _checkout_response(monkeypatch, session_payload):
    from bot_api.web import routes

    async def fake_read(_redis, _token):
        return session_payload

    monkeypatch.setattr(routes, "get_redis", lambda: None)
    monkeypatch.setattr(routes, "read_checkout_session", fake_read)
    response = _call(routes.checkout_data("tok-1"))
    return response, json.loads(response.body)


SESSION = {
    "telegram_id": 42,
    "plan": "starter",
    "period": "monthly",
    "subscription_id": "sub_LIVE",
    "amount_paise": 99_900,
}


def test_the_api_sends_everything_the_static_sites_javascript_reads():
    """The contract between two files that now deploy to different hosts.

    `web/checkout.js` runs on Cloudflare Pages and `routes.py` on the API host, so nothing
    links them at build time and nothing fails loudly when they drift. A key renamed on one
    side shows up as `undefined` printed on a payment page, which is the sort of thing you
    find out about from a customer.
    """
    source = Path("web/checkout.js").read_text(encoding="utf-8")
    read_by_js = set(re.findall(r"\bdata\.([A-Za-z_]\w*)", source))
    # `token` is put on the object by the page itself after the fetch; everything else has
    # to have come from the server.
    sent_by_api = {
        "token", "plan", "planName", "blurb", "perks", "period",
        "amountPaise", "amountRupees", "subscriptionId", "key",
        "name", "description", "botUsername",
    }
    missing = read_by_js - sent_by_api
    assert not missing, f"web/checkout.js reads keys the API never sends: {sorted(missing)}"


def test_the_api_actually_returns_those_keys(monkeypatch, secrets):
    _, body = _checkout_response(monkeypatch, SESSION)
    for key in ("plan", "planName", "blurb", "perks", "period", "amountPaise",
                "amountRupees", "subscriptionId", "key", "name", "description"):
        assert key in body, f"missing {key}"
    assert body["amountRupees"] == 999
    assert body["subscriptionId"] == "sub_LIVE"


def test_the_static_site_is_told_the_perks_rather_than_retyping_them(monkeypatch, secrets):
    """Otherwise the price rises here and the promises stay where somebody last typed them."""
    _, body = _checkout_response(monkeypatch, SESSION)
    assert tuple(body["perks"]) == STARTER.perks


def test_the_api_hands_out_the_public_key_and_never_the_secret(monkeypatch, secrets):
    _, body = _checkout_response(monkeypatch, SESSION)
    assert body["key"] == "rzp_test_key"
    assert KEY_SECRET not in json.dumps(body)


def test_an_expired_link_is_410_not_404(monkeypatch):
    """The page shows different words for the two, and sending the wrong one wastes the
    customer's time: an hour-old link needs a fresh /upgrade, a dropped connection needs a
    reload."""
    from bot_api.web import routes

    async def fake_read(_redis, _token):
        return None

    monkeypatch.setattr(routes, "get_redis", lambda: None)
    monkeypatch.setattr(routes, "read_checkout_session", fake_read)
    response = _call(routes.checkout_data("nope"))
    assert response.status_code == 410
    assert json.loads(response.body) == {"error": "expired"}


def test_the_site_rewrites_the_token_path_without_losing_it():
    """A 301 here would strip the token before any JavaScript could read it, and every
    customer would see "this link has expired"."""
    rules = Path("web/_redirects").read_text(encoding="utf-8")
    line = [ln for ln in rules.splitlines() if ln.strip().startswith("/pay/")]
    assert line, "no rewrite for /pay/<token>"
    assert line[0].split() == ["/pay/*", "/pay.html", "200"]


def test_the_shipped_config_carries_nothing_secret():
    """Everything in web/ is world-readable the moment it is deployed.

    Comment lines are dropped first, or this only tests that nobody discusses secrets in
    prose -- the wrong thing to forbid, and it would fail on the file's own warning about
    where the key_secret must never go.
    """
    source = Path("web/config.js").read_text(encoding="utf-8")
    code = " ".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("*", "/*", "*/", "//"))
    )

    assert "apiBase" in code and "botUsername" in code
    for forbidden in ("key_secret", "keySecret", "rzp_live_", "webhook_secret", "SUPABASE"):
        assert forbidden not in code, f"{forbidden} would ship to every visitor"
