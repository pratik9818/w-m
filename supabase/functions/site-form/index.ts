// Receive an enquiry from a generated site, store it, and tell the owner.
//
// This function exists because nothing else can do the job. The sites are static files on
// Cloudflare Pages with no server of their own, and bot_api has no public address to
// receive a POST at, so a form on a generated site had nowhere to send anything -- which
// is why the build prompts banned `<form>` outright for as long as they did.
//
// Three rules shape what follows:
//
//   1. **The visitor must never be told it worked when it did not.** Everything that can
//      fail before the row is written returns a real error, so the page can say "that
//      didn't send, please ring us" instead of thanking someone whose message is gone.
//      A lost enquiry leaves no trace anywhere: nobody ever finds out.
//   2. **Telling the owner is allowed to fail; storing is not.** The row is written first
//      and the Telegram message sent after, so the recoverable half of that failure is
//      the one that can happen -- an enquiry saved and unannounced, still there when they
//      ask for their data.
//   3. **The form key names the site, it does not prove anything.** It ships inside the
//      page's own script, so it is public by construction. Everything that follows treats
//      the request as hostile: sizes are capped, fields are capped, the honeypot is
//      checked, and a site being flooded is cut off before the owner's phone is.
//
// Deploy with verify_jwt disabled -- a visitor's browser has no Supabase session and never
// will, and requiring an anon key here would only mean printing one in every page.

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
// Optional. Without it the enquiry is still stored and the owner still gets it when they
// ask -- they just are not told the moment it lands.
const BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "";

// A form is a handful of short answers. Anything past these is not a customer.
const MAX_BODY_BYTES = 32 * 1024;
const MAX_FIELDS = 40;
const MAX_KEY_CHARS = 40;
const MAX_VALUE_CHARS = 4000;
const MAX_TOTAL_CHARS = 20000;

// Ceilings per site, not per visitor: an address is trivially rotated and a site is not.
// The point is to keep a flood off the owner's phone, so the burst limit is the tight one.
const MAX_PER_MINUTE = 10;
const MAX_PER_HOUR = 120;

// Filled in only by something that cannot see the page. Mirrors HONEYPOT_FIELD in
// worker/codegen/forms.py -- the two must stay in step.
const HONEYPOT_FIELD = "website";

const CORS_HEADERS = {
  // The sites live on per-business *.pages.dev hostnames and, later, the owners' own
  // domains. There is no list to check against, and no secret here to protect with one.
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Max-Age": "86400",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

async function rest(path: string, init: RequestInit = {}): Promise<Response> {
  return await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

interface Business {
  id: string;
  name: string;
  owner_telegram_id: number;
  forms: Record<string, { fields?: { name: string; label: string }[] }>;
}

async function businessForKey(formKey: string): Promise<Business | null> {
  const params = new URLSearchParams({
    select: "id,name,owner_telegram_id,forms",
    form_key: `eq.${formKey}`,
    limit: "1",
  });
  const response = await rest(`businesses?${params}`);
  if (!response.ok) return null;
  const rows = (await response.json()) as Business[];
  return rows.length ? rows[0] : null;
}

/** How many enquiries this site has taken since `since`. */
async function countSince(businessId: string, since: Date): Promise<number> {
  const params = new URLSearchParams({
    select: "id",
    business_id: `eq.${businessId}`,
    submitted_at: `gte.${since.toISOString()}`,
  });
  const response = await rest(`form_submissions?${params}`, {
    headers: { Prefer: "count=exact", Range: "0-0" },
  });
  if (!response.ok) return 0;
  // "0-0/57" -- the total is what matters, and asking for one row rather than all of them
  // keeps a flood from being expensive to measure.
  const range = response.headers.get("content-range") ?? "";
  const total = Number.parseInt(range.split("/")[1] ?? "", 10);
  return Number.isFinite(total) ? total : 0;
}

/** The submitted fields, cleaned, or null if there is nothing worth storing. */
function cleanPayload(raw: unknown): Record<string, string> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const cleaned: Record<string, string> = {};
  let total = 0;
  let filled = 0;
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (Object.keys(cleaned).length >= MAX_FIELDS) break;
    // The honeypot never reaches the table: it is a test, and storing "" on every row
    // for the life of the site is noise in the answer the owner actually reads.
    if (key === HONEYPOT_FIELD) continue;
    const name = String(key).slice(0, MAX_KEY_CHARS).trim();
    if (!name) continue;
    if (value === null || value === undefined) continue;
    if (typeof value === "object") continue;
    const text = String(value).slice(0, MAX_VALUE_CHARS).trim();
    total += name.length + text.length;
    if (total > MAX_TOTAL_CHARS) break;
    if (text) filled += 1;
    cleaned[name] = text;
  }
  // Every box left blank. Nothing was sent, whatever the browser thinks, and an empty row
  // in the owner's enquiries is worse than no row at all.
  return filled ? cleaned : null;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

/** The enquiry as the owner will read it in Telegram. */
function ownerMessage(business: Business, formName: string, payload: Record<string, string>) {
  const definition = business.forms?.[formName];
  // The owner named these fields; showing them their own words beats showing them
  // "your_name". Falls back to the key with its underscores knocked out.
  const labels = new Map<string, string>();
  for (const field of definition?.fields ?? []) {
    if (field?.name) labels.set(field.name, field.label ?? field.name);
  }

  const lines = [`📬 New enquiry from your site <b>${escapeHtml(business.name)}</b>`, ""];
  for (const [key, value] of Object.entries(payload)) {
    if (!value) continue;
    const label = labels.get(key) ?? key.replaceAll("_", " ");
    lines.push(`<b>${escapeHtml(label)}:</b> ${escapeHtml(value)}`);
  }
  lines.push("", 'Say <i>"show me my site data"</i> any time to see every enquiry.');
  // Telegram refuses anything past 4096 characters, and refusing is how the owner would
  // find out -- so a very long message is cut here rather than lost there.
  return lines.join("\n").slice(0, 3900);
}

async function notifyOwner(business: Business, formName: string, payload: Record<string, string>) {
  if (!BOT_TOKEN || !business.owner_telegram_id) return false;
  const response = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: business.owner_telegram_id,
      text: ownerMessage(business, formName, payload),
      parse_mode: "HTML",
    }),
  });
  return response.ok;
}

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (request.method !== "POST") {
    return json({ error: "method not allowed" }, 405);
  }
  if (!SUPABASE_URL || !SERVICE_KEY) {
    console.error("site-form: not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)");
    return json({ error: "not configured" }, 500);
  }

  const body = await request.text();
  if (body.length > MAX_BODY_BYTES) {
    return json({ error: "too large" }, 413);
  }

  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(body || "{}");
  } catch {
    return json({ error: "bad request" }, 400);
  }

  const formKey = String(parsed.form_key ?? "").trim();
  if (!formKey || formKey.length > 64) {
    return json({ error: "bad request" }, 400);
  }

  const business = await businessForKey(formKey);
  if (!business) {
    // A key that names no site. Usually a page from a deleted business still sitting in
    // somebody's browser tab, occasionally somebody probing.
    return json({ error: "unknown form" }, 404);
  }

  const raw = parsed.payload;
  // Checked before the honeypot so a bot filling every box in sight is refused for the
  // reason it deserves rather than being thanked.
  const honeypot = String(
    (raw && typeof raw === "object" ? (raw as Record<string, unknown>)[HONEYPOT_FIELD] : "") ?? "",
  ).trim();
  if (honeypot) {
    // Answered exactly as a real submission would be. Telling a bot it was detected only
    // teaches whoever wrote it which field to leave alone next time.
    console.log(`site-form: honeypot tripped for ${business.id}`);
    return json({ ok: true });
  }

  const payload = cleanPayload(raw);
  if (!payload) {
    return json({ error: "empty submission" }, 400);
  }

  const now = new Date();
  const lastMinute = await countSince(business.id, new Date(now.getTime() - 60_000));
  if (lastMinute >= MAX_PER_MINUTE) {
    return json({ error: "too many requests" }, 429);
  }
  const lastHour = await countSince(business.id, new Date(now.getTime() - 3_600_000));
  if (lastHour >= MAX_PER_HOUR) {
    return json({ error: "too many requests" }, 429);
  }

  const formName = String(parsed.form ?? "contact").slice(0, 40) || "contact";
  const page = String(parsed.page ?? "").slice(0, 40) || null;

  const insert = await rest("form_submissions", {
    method: "POST",
    headers: { Prefer: "return=representation" },
    body: JSON.stringify({
      business_id: business.id,
      form_name: formName,
      page,
      payload,
    }),
  });
  if (!insert.ok) {
    // The one failure the visitor has to hear about: nothing was stored, so thanking them
    // would be telling them their message arrived when it did not.
    console.error(`site-form: insert failed ${insert.status} ${await insert.text()}`);
    return json({ error: "could not save" }, 502);
  }
  const [row] = (await insert.json()) as { id: string }[];

  try {
    if (await notifyOwner(business, formName, payload)) {
      await rest(`form_submissions?id=eq.${row.id}`, {
        method: "PATCH",
        body: JSON.stringify({ notified_at: new Date().toISOString() }),
      });
    }
  } catch (error) {
    // Deliberately swallowed. The enquiry is saved; an owner who is told late is in a far
    // better position than a visitor who is told wrongly.
    console.error("site-form: notify failed", error);
  }

  return json({ ok: true });
});
