"""The two pages a customer actually sees when they pay.

Hand-written HTML with no template engine, for the same reason the generated sites carry no
build step: there are two pages, they change when the price changes, and a Jinja dependency
plus a templates directory to render 200 lines is a worse trade than a function that
returns a string.

Written for somebody standing in their shop with a phone. That drives most of the choices
here -- one screen, one price, one button, the amount repeated on the button itself so
nobody taps it wondering what they are about to be charged. The plan's own perks are read
from the catalogue rather than retyped, so this page and the `/upgrade` message can never
disagree about what ₹999 buys.

No card detail ever reaches this server: the button hands off to Razorpay's hosted
checkout, which is what keeps PCI scope at zero. The success shown here is provisional --
the entitlement is granted by the webhook, and the bot sends the real confirmation into
the chat.

Values needed by the script are passed as a JSON block rather than interpolated into the
JavaScript. That is not fastidiousness: mixing Python's brace formatting with JavaScript's
own braces is a reliable way to produce a page that renders and then does nothing when
clicked.
"""
import json
from html import escape

from bot_api.services.plans import Plan

# The one place the customer-facing look is defined. Deep pine on a warm neutral: it has to
# survive being rendered next to a bank's own UPI screen without looking like a phishing
# page, which rules out the bright-gradient styling every checkout template ships with.
_STYLE = """
:root {
  --ink: #101B1C; --ink-soft: #47585A; --ink-faint: #7A8B8C;
  --paper: #F2F4F3; --card: #FFFFFF; --rule: #DDE4E3;
  --accent: #1F5F5B; --accent-deep: #143F3C; --accent-wash: #E4EFED;
  --good: #2C6E4F; --bad: #9C3628;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--paper); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.55; -webkit-font-smoothing: antialiased;
  display: flex; justify-content: center; padding: 24px 16px 48px;
}
.wrap { width: 100%; max-width: 420px; }
.brand {
  display: flex; align-items: center; gap: 8px;
  font-size: .78rem; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-faint); margin-bottom: 14px;
}
.brand .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
.card {
  background: var(--card); border: 1px solid var(--rule); border-radius: 12px;
  overflow: hidden; box-shadow: 0 1px 2px rgba(16,27,28,.04), 0 12px 28px -18px rgba(16,27,28,.3);
}
.head { background: var(--accent-deep); color: #EEF5F4; padding: 24px 22px 22px; }
.head .plan { font-size: .76rem; letter-spacing: .13em; text-transform: uppercase; opacity: .8; }
.head .price {
  font-size: 2.6rem; font-weight: 600; letter-spacing: -.02em; line-height: 1.1; margin-top: 4px;
  font-variant-numeric: tabular-nums;
}
.head .price .per { font-size: .95rem; font-weight: 400; opacity: .78; letter-spacing: 0; }
.head .blurb { font-size: .92rem; opacity: .85; margin-top: 6px; }
.body { padding: 22px; }
ul.perks { list-style: none; margin: 0 0 22px; padding: 0; display: grid; gap: 10px; }
ul.perks li { display: grid; grid-template-columns: 18px 1fr; gap: 10px; font-size: .95rem; align-items: start; }
ul.perks li .tick { color: var(--accent); font-weight: 700; }
button.pay {
  width: 100%; border: 0; border-radius: 9px; cursor: pointer;
  background: var(--accent); color: #fff;
  font-size: 1.06rem; font-weight: 600; font-family: inherit;
  padding: 15px 18px; letter-spacing: .01em;
  transition: background .15s ease;
}
button.pay:hover { background: var(--accent-deep); }
button.pay:disabled { background: var(--ink-faint); cursor: default; }
button.pay:focus-visible { outline: 3px solid var(--accent-wash); outline-offset: 2px; }
.methods { margin-top: 14px; text-align: center; font-size: .82rem; color: var(--ink-faint); }
.methods strong { color: var(--ink-soft); font-weight: 600; }
.note {
  margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--rule);
  font-size: .84rem; color: var(--ink-faint);
}
.msg { margin-top: 14px; padding: 12px 14px; border-radius: 8px; font-size: .9rem; display: none; }
.msg.show { display: block; }
.msg.bad { background: #F7E6E3; color: var(--bad); }
.msg.info { background: var(--accent-wash); color: var(--accent-deep); }
.result { text-align: center; padding: 40px 22px; }
.result .mark {
  width: 62px; height: 62px; border-radius: 50%; margin: 0 auto 18px;
  display: grid; place-items: center; font-size: 1.9rem; color: #fff;
}
.result .mark.ok { background: var(--good); }
.result .mark.bad { background: var(--bad); }
.result h1 { font-size: 1.45rem; margin: 0 0 8px; letter-spacing: -.01em; }
.result p { margin: 0 0 8px; color: var(--ink-soft); font-size: .96rem; }
a.back {
  display: inline-block; margin-top: 20px; padding: 13px 26px; border-radius: 9px;
  background: var(--accent); color: #fff; text-decoration: none; font-weight: 600; font-size: 1rem;
}
a.back:hover { background: var(--accent-deep); }
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #E5EBEA; --ink-soft: #AEBEBC; --ink-faint: #7E908E;
    --paper: #0C1415; --card: #131E1F; --rule: #27383A;
    --accent: #3E9187; --accent-deep: #16302E; --accent-wash: #16302E;
    --good: #3F8F65; --bad: #C4574A;
  }
  .head { background: #16302E; }
  .msg.bad { background: #33201D; }
}
"""

# No braces are substituted into this: everything it needs comes from the JSON block.
_CHECKOUT_SCRIPT = """
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
(function () {
  var cfg = JSON.parse(document.getElementById("checkout-config").textContent);
  var payButton = document.getElementById("pay");
  var msg = document.getElementById("msg");

  function say(text, kind) {
    msg.textContent = text;
    msg.className = "msg show " + kind;
  }

  // What the browser reports back only decides which page to show next. The plan itself
  // is granted by Razorpay's webhook, so somebody who closes the tab at exactly the wrong
  // moment still gets what they paid for.
  function confirmPayment(response) {
    payButton.disabled = true;
    payButton.textContent = "Confirming\\u2026";
    fetch(cfg.confirmUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(response)
    }).then(function () {
      window.location.href = cfg.doneUrl;
    }).catch(function () {
      // The money is taken and the webhook lands regardless; the only thing lost here is
      // the redirect, so send them to the same place anyway.
      window.location.href = cfg.doneUrl;
    });
  }

  payButton.addEventListener("click", function () {
    var checkout = new Razorpay({
      key: cfg.key,
      subscription_id: cfg.subscriptionId,
      name: cfg.name,
      description: cfg.description,
      theme: { color: "#1F5F5B" },
      handler: confirmPayment,
      modal: {
        ondismiss: function () {
          say("Payment cancelled \\u2014 nothing has been charged. Tap the button to try again.", "info");
        }
      }
    });
    checkout.on("payment.failed", function (event) {
      var reason = (event && event.error && event.error.description)
        ? event.error.description
        : "the payment did not go through";
      say("Couldn't take the payment: " + reason + ". Nothing has been charged \\u2014 try again, or use a different method.", "bad");
    });
    checkout.open();
  });
})();
</script>
"""


def _json_block(element_id: str, data: dict) -> str:
    """Embed data for the script to read, without it ever being parsed as JavaScript.

    The `</` replacement is the one thing `json.dumps` will not do for you: a value
    containing `</script>` would otherwise close the block early.
    """
    payload = json.dumps(data).replace("</", "<\\/")
    return f'<script id="{element_id}" type="application/json">{payload}</script>'


def _shell(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)}</title>"
        f"<style>{_STYLE}</style>"
        f'</head><body><div class="wrap">{body}</div></body></html>'
    )


def render_checkout(
    *,
    plan: Plan,
    period: str,
    amount_paise: int,
    subscription_id: str,
    razorpay_key_id: str,
    token: str,
    business_name: str | None = None,
) -> str:
    rupees = amount_paise // 100
    per = "a month" if period == "monthly" else "a year"
    perks = "".join(
        f'<li><span class="tick">✓</span><span>{escape(p)}</span></li>' for p in plan.perks
    )
    for_line = (
        f'<div class="note">This is for <strong>{escape(business_name)}</strong>.</div>'
        if business_name
        else ""
    )
    config = _json_block("checkout-config", {
        "key": razorpay_key_id,
        "subscriptionId": subscription_id,
        "name": business_name or "Your website",
        "description": f"{plan.name} plan, billed {per}",
        "confirmUrl": f"/pay/{token}/confirm",
        "doneUrl": f"/pay/{token}/done",
    })

    body = f"""
<div class="brand"><span class="dot"></span><span>Website plan</span></div>
<div class="card">
  <div class="head">
    <div class="plan">{escape(plan.name)}</div>
    <div class="price">₹{rupees:,}<span class="per"> {escape(per)}</span></div>
    <div class="blurb">{escape(plan.blurb)}</div>
  </div>
  <div class="body">
    <ul class="perks">{perks}</ul>
    <button class="pay" id="pay" type="button">Pay ₹{rupees:,}</button>
    <div class="methods">
      Pay by <strong>UPI</strong>, card, net banking or wallet.<br>
      Renews automatically. Cancel any time from the chat.
    </div>
    <div class="msg" id="msg"></div>
    {for_line}
    <div class="note">
      Payment is handled by Razorpay. Your card and UPI details never reach us.
    </div>
  </div>
</div>
{config}
{_CHECKOUT_SCRIPT}
"""
    return _shell(f"Pay ₹{rupees:,} — {plan.name}", body)


def render_result(
    *, ok: bool, headline: str, detail: str, bot_username: str | None = None
) -> str:
    back = (
        f'<a class="back" href="https://t.me/{escape(bot_username)}">Back to the chat</a>'
        if bot_username
        else ""
    )
    mark_class = "ok" if ok else "bad"
    mark_glyph = "✓" if ok else "!"
    body = f"""
<div class="card">
  <div class="result">
    <div class="mark {mark_class}">{mark_glyph}</div>
    <h1>{escape(headline)}</h1>
    <p>{escape(detail)}</p>
    {back}
  </div>
</div>
"""
    return _shell(headline, body)
