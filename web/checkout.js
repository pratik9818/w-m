/* The payment page's whole behaviour.
 *
 * What this file may and may not do is the important part. It may decide what the
 * customer sees. It may not decide what they are entitled to: the subscription was
 * created on the API host before this page was ever opened, and the plan is granted by
 * Razorpay's webhook afterwards. Everything here is presentation over a decision made
 * somewhere the customer's browser cannot reach.
 *
 * That is also why nothing here is trusted on the way back. The POST to /confirm is not
 * what unlocks the plan -- it only lets the success page be shown honestly rather than
 * optimistically. Someone who closes the tab at exactly the wrong moment still gets what
 * they paid for; the confirmation simply arrives in Telegram instead of on screen.
 */
(function () {
  "use strict";

  var cfg = window.CHECKOUT || {};
  var apiBase = String(cfg.apiBase || "").replace(/\/+$/, "");

  var el = {
    loading: document.getElementById("state-loading"),
    ready: document.getElementById("state-ready"),
    expired: document.getElementById("state-expired"),
    planName: document.getElementById("plan-name"),
    price: document.getElementById("price"),
    per: document.getElementById("per"),
    blurb: document.getElementById("blurb"),
    perks: document.getElementById("perks"),
    pay: document.getElementById("pay"),
    msg: document.getElementById("msg"),
    note: document.getElementById("note"),
    backExpired: document.getElementById("back-expired")
  };

  var rupees = new Intl.NumberFormat("en-IN");

  function show(state) {
    el.loading.hidden = state !== "loading";
    el.ready.hidden = state !== "ready";
    el.expired.hidden = state !== "expired";
  }

  function say(text, kind) {
    el.msg.textContent = text;
    el.msg.className = "msg show " + kind;
  }

  function botLink() {
    return cfg.botUsername ? "https://t.me/" + encodeURIComponent(cfg.botUsername) : null;
  }

  /* The token travels in the path (/pay/<token>) because that is what the customer sees in
   * the address bar, and a path reads as an address while ?t=... reads as something that
   * can be edited. The query form is accepted too, for hosts with no rewrite rules. */
  function readToken() {
    var match = window.location.pathname.match(/\/pay\/([^/?#]+)/);
    if (match) return decodeURIComponent(match[1]);
    return new URLSearchParams(window.location.search).get("t");
  }

  function expired() {
    var link = botLink();
    if (link) {
      el.backExpired.href = link;
      el.backExpired.hidden = false;
    }
    show("expired");
  }

  function render(data) {
    document.title = "Pay ₹" + rupees.format(data.amountRupees) + " — " + data.planName;

    el.planName.textContent = data.planName;
    el.price.textContent = "₹" + rupees.format(data.amountRupees);
    el.per.textContent = data.period === "yearly" ? "a year" : "a month";
    el.blurb.textContent = data.blurb || "";

    /* textContent throughout, never innerHTML. The perks come from our own catalogue
     * today, but a page that only stays safe while its data source stays trustworthy is
     * one refactor away from not being safe. */
    el.perks.innerHTML = "";
    (data.perks || []).forEach(function (perk) {
      var li = document.createElement("li");
      var tick = document.createElement("span");
      tick.className = "tick";
      tick.textContent = "✓";
      var text = document.createElement("span");
      text.textContent = perk;
      li.appendChild(tick);
      li.appendChild(text);
      el.perks.appendChild(li);
    });

    // The amount goes on the button as well as in the header. Nobody should tap a payment
    // button while wondering what they are about to be charged.
    el.pay.textContent = "Pay ₹" + rupees.format(data.amountRupees);

    var renewal = data.period === "yearly" ? "Renews once a year." : "Renews every month.";
    el.note.textContent = renewal
      + " You can cancel any time from the chat, and your website stays online either way."
      + " Payment is handled by Razorpay — your card and UPI details never reach us.";

    show("ready");
    el.pay.addEventListener("click", function () { openCheckout(data); });
  }

  function confirmPayment(token, planName, response) {
    el.pay.disabled = true;
    el.pay.textContent = "Confirming…";
    var done = "/done.html?plan=" + encodeURIComponent(planName);

    fetch(apiBase + "/api/checkout/" + encodeURIComponent(token) + "/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(response)
    }).then(function () {
      window.location.href = done;
    }).catch(function () {
      // The money has moved and the webhook lands regardless. The only thing lost to a
      // failed fetch here is the redirect, so send them to the same place anyway.
      window.location.href = done;
    });
  }

  function openCheckout(data) {
    if (typeof window.Razorpay !== "function") {
      say("The payment window could not load. Check your connection and try again.", "bad");
      return;
    }
    var checkout = new window.Razorpay({
      key: data.key,
      subscription_id: data.subscriptionId,
      name: data.name,
      description: data.description,
      theme: { color: "#1F5F5B" },
      handler: function (response) { confirmPayment(data.token, data.planName, response); },
      modal: {
        ondismiss: function () {
          say("Payment cancelled — nothing has been charged. Tap the button to try again.", "info");
        }
      }
    });
    checkout.on("payment.failed", function (event) {
      var reason = (event && event.error && event.error.description)
        ? event.error.description
        : "the payment did not go through";
      say("Couldn't take the payment: " + reason + ". Nothing has been charged — try again, "
          + "or use a different method.", "bad");
    });
    checkout.open();
  }

  function start() {
    var token = readToken();
    if (!token) { expired(); return; }

    if (!apiBase) {
      // A deployment mistake, not a customer mistake. Say so plainly rather than showing
      // the expired page, which would send them back to the bot for a link that would
      // fail in exactly the same way.
      show("ready");
      say("This page is not configured yet (apiBase is empty in config.js).", "bad");
      return;
    }

    fetch(apiBase + "/api/checkout/" + encodeURIComponent(token), {
      headers: { "Accept": "application/json" }
    }).then(function (res) {
      if (res.status === 404 || res.status === 410) { expired(); return null; }
      if (!res.ok) throw new Error("http " + res.status);
      return res.json();
    }).then(function (data) {
      if (!data) return;
      data.token = token;
      render(data);
    }).catch(function () {
      // Distinct from expired on purpose: an hour-old link and a dropped connection need
      // different things from the customer, and telling them to fetch a new link when the
      // old one is fine wastes their time.
      show("ready");
      el.pay.disabled = true;
      el.pay.textContent = "Unavailable";
      say("Couldn't load your plan details just now. Nothing has been charged — check your "
          + "connection and reload this page.", "bad");
    });
  }

  start();
})();
