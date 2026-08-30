/* The two values this site cannot work out for itself. Edit before deploying.
 *
 * Kept as a separate file, not baked into checkout.js, so that changing where the API
 * lives is a one-line edit you can make in the host's dashboard without a rebuild.
 *
 * Neither value is a secret. `apiBase` is a public address and Razorpay's key_id is
 * public by design -- it identifies the merchant, it does not authorise anything. The
 * key_secret never comes anywhere near this folder; it lives only on the API host, which
 * is the whole reason the subscription is created there rather than here.
 */
window.CHECKOUT = {
  // Where the bot's API is reachable. No trailing slash. Must be https, and must match
  // CHECKOUT_SITE_URL's counterpart in the API's .env or the browser will block the
  // request as a CORS failure.
  apiBase: "https://your-app.vercel.app",

  // Without the @. Used for the "Back to the chat" buttons.
  botUsername: "teko21bot"
};
