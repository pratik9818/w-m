"""The payment page, and the webhook that is the only thing allowed to grant a plan.

Three of these routes are for the customer's browser and one is for Razorpay's servers, and
they are held to different standards. The browser routes may fail visibly -- somebody can
tap again. The webhook may not: it is how money becomes entitlement, so it verifies before
it parses, it never trusts what the request says about who is paying beyond the signature
that proves Razorpay sent it, and it answers non-2xx when it genuinely failed, because a
non-2xx is what makes Razorpay try again.

The link handed into the chat carries an opaque token, never a Telegram id. Chat messages
get forwarded and screenshotted; the token is random, expires in an hour, and means nothing
outside Redis.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bot_api.config import get_settings
from bot_api.services.billing import (
    read_checkout_session,
    verify_checkout_signature,
    verify_webhook_signature,
)
from bot_api.services.billing_events import handle_event
from bot_api.services.plans import get_plan
from bot_api.services.redis_client import get_redis
from bot_api.web.pages import render_checkout, render_result
from db.base import session_scope

logger = logging.getLogger(__name__)

router = APIRouter()


def _expired_page() -> HTMLResponse:
    settings = get_settings()
    # 200 rather than 404: this is a page a person is reading, and a browser's own error
    # page instead of an explanation is a lost customer.
    return HTMLResponse(
        render_result(
            ok=False,
            headline="This payment link has expired",
            detail=(
                "Payment links last an hour, for safety. Nothing has been charged — go back "
                "to the chat and send /upgrade to get a fresh one."
            ),
            bot_username=settings.bot_username or None,
        )
    )


@router.get("/pay/{token}", response_class=HTMLResponse)
async def payment_page(token: str) -> HTMLResponse:
    session = await read_checkout_session(get_redis(), token)
    if session is None:
        return _expired_page()

    settings = get_settings()
    plan = get_plan(session["plan"])
    return HTMLResponse(render_checkout(
        plan=plan,
        period=session["period"],
        amount_paise=session["amount_paise"],
        subscription_id=session["subscription_id"],
        razorpay_key_id=settings.razorpay_key_id,
        token=token,
        business_name=session.get("business_name"),
    ))


async def _confirm(token: str, request: Request) -> JSONResponse:
    """What the browser reports after checkout closes successfully.

    Grants nothing. Its only job is to let the success page be shown honestly rather than
    optimistically -- the signature check is what distinguishes "Razorpay really did take
    the money" from "somebody posted to this URL". The plan is granted by the webhook.

    Shared by the server-rendered page and the standalone site in web/, because the two
    front ends must not be able to disagree about what counts as a verified payment.
    """
    session = await read_checkout_session(get_redis(), token)
    if session is None:
        raise HTTPException(status_code=410, detail="checkout session expired")

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="expected JSON")

    payment_id = str(body.get("razorpay_payment_id") or "")
    subscription_id = str(body.get("razorpay_subscription_id") or "")
    signature = str(body.get("razorpay_signature") or "")

    if subscription_id != session["subscription_id"]:
        # The token and the subscription have to agree. They would not if a link were
        # replayed against somebody else's session.
        logger.warning(
            "billing.confirm_mismatch",
            extra={"event": "billing.confirm_mismatch", "token_subscription": session["subscription_id"]},
        )
        raise HTTPException(status_code=400, detail="subscription mismatch")

    verified = bool(payment_id and signature) and verify_checkout_signature(
        payment_id, subscription_id, signature
    )
    if not verified:
        logger.warning(
            "billing.confirm_unverified",
            extra={"event": "billing.confirm_unverified", "subscription_id": subscription_id},
        )
    return JSONResponse({"ok": verified})


@router.post("/pay/{token}/confirm")
async def payment_confirm(token: str, request: Request) -> JSONResponse:
    return await _confirm(token, request)


@router.get("/pay/{token}/done", response_class=HTMLResponse)
async def payment_done(token: str) -> HTMLResponse:
    settings = get_settings()
    session = await read_checkout_session(get_redis(), token)
    plan_name = get_plan(session["plan"]).name if session else "your plan"
    return HTMLResponse(render_result(
        ok=True,
        headline="Payment received",
        detail=(
            f"You're on {plan_name}. Go back to the chat — a confirmation is waiting there, "
            "usually within a few seconds."
        ),
        bot_username=settings.bot_username or None,
    ))


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request) -> JSONResponse:
    # The raw bytes, before anything else touches them. Parsing the JSON and
    # re-serialising it to check the signature changes key order and whitespace, and then
    # every legitimate event fails verification with nothing in the logs to explain it.
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature")

    if not verify_webhook_signature(raw, signature):
        logger.warning(
            "billing.webhook_bad_signature",
            extra={"event": "billing.webhook_bad_signature", "bytes": len(raw)},
        )
        raise HTTPException(status_code=400, detail="bad signature")

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="expected JSON")

    event = str(body.get("event") or "")
    # Razorpay's own id for this delivery, which is what makes retries harmless. The
    # composite fallback exists because a missing header would otherwise make every
    # delivery look unique and grant a month per retry.
    event_id = request.headers.get("x-razorpay-event-id") or _fallback_event_id(event, body)

    async with session_scope() as session:
        message = await handle_event(session, event_id, event, body.get("payload") or {})
        owner = (
            ((body.get("payload") or {}).get("subscription") or {}).get("entity") or {}
        ).get("notes", {}).get("telegram_id")

    if message and owner:
        # After the commit, never inside it: an owner told their plan is active by a
        # transaction that then rolls back has been lied to in the most expensive way
        # available. A failure to deliver is logged and swallowed -- the money is banked
        # and the entitlement is granted, so a 500 here would only buy a duplicate event.
        try:
            from bot_api.bot.bot import get_bot

            await get_bot().send_message(int(owner), message)
        except Exception:
            logger.exception("failed to notify %s about %s", owner, event)

    return JSONResponse({"ok": True})


def _fallback_event_id(event: str, body: dict) -> str:
    """A stable id for a delivery that arrived without one.

    Built from the things that identify the *occurrence* rather than the delivery: the
    subscription, and the billing cycle it is about. Two retries of one charge produce the
    same string; next month's charge does not.
    """
    entity = ((body.get("payload") or {}).get("subscription") or {}).get("entity") or {}
    payment = ((body.get("payload") or {}).get("payment") or {}).get("entity") or {}
    return ":".join(str(part) for part in (
        event,
        entity.get("id") or "-",
        payment.get("id") or entity.get("current_start") or body.get("created_at") or "-",
    ))[:80]


# ------------------------------------------------------------------ the JSON checkout API
#
# These two exist for the standalone payment site in web/, which is hosted somewhere else
# and so cannot be handed a server-rendered page. The split is not cosmetic: a static site
# cannot create a Razorpay subscription, because doing that needs the key_secret, and a
# secret shipped to a browser is not a secret. So the subscription is created here when the
# owner taps a plan in the chat, and the site is only ever told what it needs to draw
# itself and open the checkout overlay.
#
# Nothing returned here is confidential. `key` is Razorpay's key_id, which identifies the
# merchant and authorises nothing; the amount and plan are what the customer is about to be
# shown anyway. The token is the access control: 24 random bytes, an hour to live, and
# meaningless outside Redis.


@router.get("/api/checkout/{token}")
async def checkout_data(token: str) -> JSONResponse:
    session = await read_checkout_session(get_redis(), token)
    if session is None:
        # 410 rather than 404: the distinction matters to the page, which shows "this link
        # has expired" for one and "something went wrong, try reloading" for the other.
        return JSONResponse({"error": "expired"}, status_code=410)

    settings = get_settings()
    plan = get_plan(session["plan"])
    period = session["period"]
    per = "a month" if period == "monthly" else "a year"
    business_name = session.get("business_name")

    return JSONResponse({
        "token": token,
        "plan": plan.code,
        "planName": plan.name,
        "blurb": plan.blurb,
        # Read from the catalogue, never retyped into the static site. The page and the
        # /upgrade message cannot then disagree about what a customer is buying.
        "perks": list(plan.perks),
        "period": period,
        "amountPaise": session["amount_paise"],
        "amountRupees": session["amount_paise"] // 100,
        "subscriptionId": session["subscription_id"],
        "key": settings.razorpay_key_id,
        "name": business_name or "Your website",
        "description": f"{plan.name} plan, billed {per}",
        "botUsername": settings.bot_username or None,
    })


@router.post("/api/checkout/{token}/confirm")
async def checkout_confirm(token: str, request: Request) -> JSONResponse:
    return await _confirm(token, request)
