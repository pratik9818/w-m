"""Razorpay: creating a subscription, and proving that a message about one is genuine.

Razorpay rather than the alternatives for three reasons that matter here specifically.
It supports **UPI Autopay** mandates, which is the only recurring rail an Indian shop
owner will actually complete -- card-on-file recurring in India needs the customer to
re-authorise at every bank's own pace, and e-NACH takes days to register. It has a hosted
checkout, so no card detail ever touches our server and PCI scope stays at zero. And its
subscription objects carry a `notes` dictionary that survives the whole lifecycle, which
is what lets a webhook arriving days later know which Telegram account paid without the
customer ever coming back to the chat.

Cashfree is the closest substitute and would need only this file rewritten. Stripe is not
a real option for domestic Indian recurring payments; Telegram Stars is compliant and
native but takes roughly a third of the price, which is why the money leaves Telegram.

Plain httpx rather than the `razorpay` SDK, matching how Cloudflare is called in
worker/tasks/deploy.py: the surface used here is four REST calls and two HMACs, and a
dependency that must be installed into a serverless runtime to do that is a poor trade.

Two signatures, and they are not the same construction -- this is the detail that costs
people an afternoon:

  - the **webhook** signature is HMAC-SHA256 over the raw request body, keyed with the
    webhook secret;
  - the **checkout callback** signature for a subscription is HMAC-SHA256 over
    `payment_id|subscription_id`, keyed with the API secret. For a one-off order the two
    halves are the other way round. Getting the order wrong fails every payment silently.
"""
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone

import httpx

from bot_api.config import get_settings
from bot_api.services.plans import Plan, price_paise

logger = logging.getLogger(__name__)

API_BASE = "https://api.razorpay.com/v1"
REQUEST_TIMEOUT = 20.0

# How many cycles a mandate is authorised for. Razorpay requires a finite number, so this
# is "long enough that nobody reaches it" rather than a real limit -- ten years of months,
# ten years of years. A subscription that ends because it hit its count would look to the
# customer like an unexplained cancellation.
TOTAL_COUNT = {"monthly": 120, "yearly": 10}

# How long a payment link stays usable. Long enough to find your phone and your UPI PIN,
# short enough that a link forwarded to a group chat is not a free upgrade.
CHECKOUT_TTL_SECONDS = 60 * 60


class BillingNotConfigured(Exception):
    """No Razorpay credentials. Distinct from a failure, because the fix is different."""


class BillingCallFailed(Exception):
    pass


def is_configured() -> bool:
    s = get_settings()
    return bool(s.razorpay_key_id and s.razorpay_key_secret)


def razorpay_plan_id(plan: Plan, period: str) -> str:
    """The id of the matching Plan object created once in the Razorpay dashboard.

    Kept in configuration rather than created on the fly: a Razorpay plan is immutable
    once it has subscribers, so creating them from code would mean a price change silently
    orphaning everybody on the old one.
    """
    s = get_settings()
    key = f"razorpay_plan_{plan.code}_{period}"
    plan_id = getattr(s, key, "")
    if not plan_id:
        raise BillingNotConfigured(f"{key.upper()} is not set")
    return plan_id


def _auth() -> tuple[str, str]:
    s = get_settings()
    if not (s.razorpay_key_id and s.razorpay_key_secret):
        raise BillingNotConfigured("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set")
    return s.razorpay_key_id, s.razorpay_key_secret


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(f"{API_BASE}{path}", json=body, auth=_auth())
    if response.status_code >= 400:
        # Razorpay's error bodies are genuinely useful ("plan_id is invalid"), and losing
        # them to a bare status code turns a five-minute fix into a guessing game.
        logger.error(
            "razorpay.error",
            extra={"event": "razorpay.error", "path": path,
                   "status": response.status_code, "body": response.text[:500]},
        )
        raise BillingCallFailed(f"Razorpay {path} returned {response.status_code}")
    return response.json()


async def _get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(f"{API_BASE}{path}", auth=_auth())
    if response.status_code >= 400:
        raise BillingCallFailed(f"Razorpay {path} returned {response.status_code}")
    return response.json()


async def create_subscription(
    plan: Plan, period: str, telegram_id: int, telegram_username: str | None = None
) -> dict:
    """Open a subscription in Razorpay, tagged with who it belongs to.

    `notes` is the whole reason activation works without the customer returning to the
    chat: it comes back on every future webhook for this subscription, so a renewal
    fourteen months from now still knows which Telegram account to credit and message.
    """
    body = {
        "plan_id": razorpay_plan_id(plan, period),
        "total_count": TOTAL_COUNT.get(period, 120),
        "quantity": 1,
        # Razorpay's own email/SMS reminders. Off: the bot is the channel, and a duplicate
        # notification from a name the customer does not recognise reads as a scam.
        "customer_notify": 0,
        "notes": {
            "telegram_id": str(telegram_id),
            "telegram_username": telegram_username or "",
            "plan": plan.code,
            "period": period,
        },
    }
    subscription = await _post("/subscriptions", body)
    logger.info(
        "billing.subscription_created",
        extra={"event": "billing.subscription_created", "owner": telegram_id,
               "plan": plan.code, "period": period, "subscription_id": subscription.get("id")},
    )
    return subscription


async def fetch_subscription(subscription_id: str) -> dict:
    return await _get(f"/subscriptions/{subscription_id}")


async def cancel_subscription(subscription_id: str, at_cycle_end: bool = True) -> dict:
    """Stop future charges. At cycle end by default -- they paid for this month."""
    return await _post(
        f"/subscriptions/{subscription_id}/cancel",
        {"cancel_at_cycle_end": 1 if at_cycle_end else 0},
    )


# ---------------------------------------------------------------- signatures


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 over the **raw bytes**, keyed with the webhook secret.

    It has to be the exact bytes Razorpay sent. Parsing the JSON and re-serialising it
    changes key order and whitespace, and every signature then fails with no clue as to
    why -- which is why the route reads `await request.body()` before it reads anything
    else.
    """
    s = get_settings()
    if not s.razorpay_webhook_secret or not signature:
        return False
    expected = hmac.new(
        s.razorpay_webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_checkout_signature(
    payment_id: str, subscription_id: str, signature: str
) -> bool:
    """The signature the browser hands back when checkout succeeds.

    For a subscription the payload is `payment_id|subscription_id`. For a one-off order it
    is `order_id|payment_id` -- the opposite order. This only confirms the browser is
    telling the truth so the success page can be shown immediately; entitlement is granted
    by the webhook, which is the only source that cannot be replayed by a customer.
    """
    s = get_settings()
    if not s.razorpay_key_secret:
        return False
    expected = hmac.new(
        s.razorpay_key_secret.encode(),
        f"{payment_id}|{subscription_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------- checkout sessions


def _session_key(token: str) -> str:
    return f"checkout:{token}"


async def open_checkout_session(
    redis, plan: Plan, period: str, telegram_id: int, telegram_username: str | None = None
) -> tuple[str, dict]:
    """Create the subscription and stash everything the payment page needs to render it.

    Returns an opaque token, not the Telegram id. The payment URL is sent into a chat that
    can be forwarded, screenshotted or shoulder-surfed, so it must not contain anything
    worth reading: the token is random, short-lived, and means nothing outside Redis.
    """
    subscription = await create_subscription(plan, period, telegram_id, telegram_username)
    token = secrets.token_urlsafe(24)
    payload = {
        "telegram_id": telegram_id,
        "plan": plan.code,
        "period": period,
        "subscription_id": subscription["id"],
        "amount_paise": price_paise(plan, period),
    }
    await redis.set(_session_key(token), json.dumps(payload), ex=CHECKOUT_TTL_SECONDS)
    return token, payload


async def read_checkout_session(redis, token: str) -> dict | None:
    raw = await redis.get(_session_key(token))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------- event helpers


def unix_to_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def owner_from_notes(entity: dict) -> int | None:
    """Pull the Telegram id back out of a Razorpay entity's notes."""
    notes = entity.get("notes") or {}
    raw = notes.get("telegram_id")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("razorpay notes carried an unusable telegram_id: %r", raw)
        return None
