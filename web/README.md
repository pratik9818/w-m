# The payment site

A static site. Five files, no build step, no dependencies. It shows somebody the plan they
are about to buy and opens Razorpay's checkout over the top of it.

## Why it cannot be static all the way down

A page served from a static host cannot create a Razorpay subscription. Creating one takes
the `key_secret`, and a secret shipped to a browser has stopped being a secret the moment
it arrives. So the work is split:

| Where | What it does | What it holds |
|---|---|---|
| The bot's API | creates the subscription when somebody taps a plan in the chat; receives Razorpay's webhook; grants the plan | `key_secret`, `webhook_secret`, the database |
| This site | shows the plan, opens the checkout overlay | nothing secret |

The token in the URL is the whole of the access control: 24 random bytes, one hour to
live, meaningless outside the API's Redis. That is deliberate — a payment link travels
through a chat that gets forwarded and screenshotted, so it must not contain a Telegram id
or anything else worth reading.

**The webhook cannot live here.** It is a POST from Razorpay's servers to a URL that has
to verify an HMAC and write to the database. It stays on the API host. If you take one
thing from this file, take that one: a customer can pay successfully and still never get
their plan if the webhook is pointed at this site instead of at the API.

## Files

```
pay.html      the payment page      (served for /pay/<token>)
done.html     "payment received"    (after checkout closes)
index.html    someone typed the bare domain in
checkout.js   all the behaviour
config.js     the two values you must edit
styles.css    the look
_redirects    Cloudflare Pages / Netlify rewrite for /pay/<token>
```

## Deploying it

**1. Edit `config.js`.** Both values are public; neither is a secret.

```js
window.CHECKOUT = {
  apiBase: "https://your-app.vercel.app",   // where the bot's API lives, no trailing slash
  botUsername: "teko21bot"                  // without the @
};
```

**2. Deploy this folder.** On Cloudflare Pages: create a project from the repo, set the
build output directory to `web`, and leave the build command empty. `_redirects` is picked
up automatically. Netlify works the same way with the same file.

On Vercel instead, delete `_redirects` and add `web/vercel.json`:

```json
{ "rewrites": [{ "source": "/pay/:token", "destination": "/pay.html" }] }
```

The rewrite must return **200, not a redirect**. A redirect throws the token out of the
URL before the JavaScript can read it, and every customer sees "this link has expired".

**3. Point the API at it.** In the API's `.env`:

```
CHECKOUT_SITE_URL=https://pay.example.com
```

That does two things: the link the bot sends now points here, and this origin becomes the
only one allowed to call `/api/checkout` from a browser. It must match the deployed origin
exactly — scheme, host, no trailing slash — or every request fails as a CORS error, which
in a browser looks identical to the API being down.

**4. Redeploy the API** so it picks up the new origin, and check the round trip:

```
curl -i https://your-app.vercel.app/api/checkout/definitely-not-a-real-token
# expect: HTTP/1.1 410  {"error":"expired"}
```

A 410 means the route is live and Redis answered. A 404 means the API has not been
redeployed. No CORS headers on a real request means `CHECKOUT_SITE_URL` does not match.

## What a customer actually goes through

```
Telegram              this site                     Razorpay
/upgrade
  tap Starter  ──►  (API creates subscription)
  tap "Pay ₹999" ──►  /pay/<token>
                      GET  api/checkout/<token> ──►
                   ◄── plan, price, subscription id
                      tap Pay ₹999          ──────────►  overlay: UPI / card / netbanking
                   ◄──────────────────────────────────   paid
                      POST api/checkout/<token>/confirm
                      /done.html
                                                          webhook ──► API grants the plan
  ◄── "You're on Starter" (sent by the webhook, not by this site)
```

The confirmation in the chat comes from the webhook, never from this site. That is why
somebody who closes the tab the instant after paying still gets what they paid for.

## Editing it

Open `pay.html?t=anything` against a running API and you will get the expired card, which
is the quickest way to check styling. For the full page you need a live token: send
`/upgrade` to the bot and use the link it gives you.

`styles.css` is a copy of the palette in `bot_api/web/pages.py`, which renders the
fallback pages served from the API when `CHECKOUT_SITE_URL` is unset. They are separate
files on purpose — this one ships to a different host — so a palette change needs making
in both.
