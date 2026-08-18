# Project Status

Full architecture/design doc: `C:\Users\Prahlad Singh\.claude\plans\let-continure-expressive-dream.md`
(Telegram bot → LLM generates a real website per business → tested in a sandbox → auto-deployed → link sent in chat. Built in phases; **Parts 1, 2, 3, 4a, 4b and 6 are done and verified live. The LLM provider for Part 2 and Part 4b was swapped from Gemini to OpenRouter mid-session; Part 6 then replaced the single-page generator with a parallel four-page one — see both notes below.**)

> **Read Part 6 first if you are looking at generation, sandboxing, or deploy code.** It supersedes the "two files / single page / 7 checks" descriptions in the Part 2, 3 and 4a sections below, which are kept as history rather than rewritten.

## Done

**Part 1 — Telegram integration only (no site generation yet)**

- Project scaffold: `pyproject.toml`, `vercel.json`, `requirements.txt`, folder layout (`bot_api/`, `db/`, `worker/` stub, `alembic/`)
- Database schema written (not yet applied to a live database — see Pending):
  - SQLAlchemy models: `db/models/{business,service,media,site_version,edit_log}.py`
  - Alembic migration: `alembic/versions/0001_initial_schema.py`
  - Plain SQL version for manual/dashboard use: `db/schema.sql`
- Telegram bot (aiogram 3), all import-checked and working locally:
  - `/start`, `/help`, `/cancel`
  - `/newsite`, `/mysites` — multi-site support, one Telegram user can own several businesses
  - Active-site tracking in Redis so free-text messages later know which site they apply to
  - Full onboarding conversation: name → category → tagline → about → services (loop, up to 15) → phone/email/address → hours → logo → up to 5 photos → theme (classic/modern/bold) → summary → confirm. On confirm, writes the business + services + media to Postgres and sets `generation_status='queued'`.
  - Photo/logo uploads go to Supabase Storage (bucket `business-media`), 5MB limit, jpeg/png/webp only
  - Catch-all handler for messages outside any flow — currently just tells the owner their site's status; real natural-language editing comes in a later part
- FastAPI app (`bot_api/main.py`): `/health` endpoint, Telegram webhook route at `/telegram/webhook/{secret}` — verified locally that it boots and correctly rejects requests with the wrong secret (404)
- `scripts/set_webhook.py` — one-off script to register the webhook URL with Telegram once deployed
- Verified: all modules import cleanly, SQLAlchemy models register correctly, FastAPI app boots and responds

## Pending — blocking Part 1 completion

Done:
- ✅ Supabase project created by you; schema applied (all 5 tables live), Alembic stamped to `0001` so future migrations won't conflict
- ✅ `business-media` public Storage bucket created
- ✅ `.env` populated with `DATABASE_URL` (via the connection **pooler**, not direct — see note below), `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`

Note: Supabase's direct DB host (`db.<ref>.supabase.co`) is IPv6-only and didn't resolve on this network, so we're using the Supavisor **transaction pooler** (`aws-0-ap-south-1.pooler.supabase.com:6543`) instead — works fine for this use case.

- ✅ Upstash Redis created; `REDIS_URL` in `.env`, connection tested (ping + set/get both succeeded)
- ✅ **Full local smoke test in real Telegram**, via `scripts/run_polling.py` (long-polling, no deployment needed for this). Bot is `@teko21bot`. Ran the complete onboarding flow live — a real business ("Chai wala", Restaurant/Cafe) was created and confirmed present in Supabase with `generation_status='queued'`, correct slug, and no errors in ~17 handled updates. This proves the FSM, Redis session/state, validation, and Postgres writes all work correctly end-to-end.
- Fixed a latent bug found along the way: `db/base.py`'s engine now passes `connect_args={"statement_cache_size": 0}` — required for asyncpg to work reliably against Supabase's pgbouncer transaction-mode pooler (which is what we're using, since the direct IPv6 host isn't reachable here).

Still waiting on you for:
1. **Vercel account** — to actually deploy `bot_api` and switch from local polling to the real webhook (Task #6 below). Not strictly required to keep developing — Part 2 (codegen) doesn't depend on it — but needed before this can run as an always-on service instead of on your machine.

## Part 2 — Code generation harness (done and verified live)

Standalone codegen pipeline — not wired to the bot/Telegram yet, per the phased plan. Fully built and **verified against the real API**:

- `worker/codegen/builder.py` — `spec_from_business()` flattens a saved business into a plain dict; `build_site()` calls **Gemini** (see provider note below) with a forced `write_site` function call to get back exactly `index.html` + `style.css` (single-page site, CSS-only responsive, no JS, static `tel:`/`mailto:` contact links — no form/backend wiring yet, that's Part 4). Retries up to 3x with backoff on technical failures (API errors/timeouts/malformed response) only — no self-review loop.
- `worker/codegen/prompts/site_builder.md` — one flexible prompt with short style guidance per theme (classic/modern/bold); category-specific content comes from the business's own data, not per-category templates.
- `worker/codegen/samples.py` — 2 canned sample specs (plumber/modern, hair salon/bold) for testing variety without needing more real Telegram data.
- **Token quota system** (`worker/codegen/quota.py`) — `token_usage` table in Supabase, one row per generation call. Each Telegram owner gets a **300,000-token free-tier budget** shared across all their businesses. Generation is refused with a clear message once exhausted — first piece of a future paid-plan system (the existing unused `businesses.plan` column is the hook for that later).
- `scripts/generate_site.py` — CLI: `--business-id <uuid>` (pulls a real saved business, checks/consumes quota) or `--sample plumber|salon` (no quota, for eyeballing variety). Writes output to `generated_output/<name>/` and prints token usage + running quota total.

**Provider note**: originally built against Gemini (Anthropic had no credit balance), then swapped to **OpenRouter** mid-session after Gemini's 20-requests/day free cap became a real blocker — see the "Provider swap: Gemini → OpenRouter" section below for the full story. `build_site()`'s external interface is unchanged either way.

**Verified live** — ran against the real "Chai wala" business and both sample specs:
- Output is clean, semantic, professional HTML/CSS; themes are visibly distinct (modern = clean/light, bold = dark with a vivid accent)
- No fabricated content — every generation faithfully reflects only the data actually in the spec
- Zero `<form>` elements or `fetch()` calls anywhere — contact info renders as static `tel:`/`mailto:` links only, confirmed via grep across all output
- `token_usage` row written correctly (794 in + 3654 out, `gemini-3.7-flash`, correct `owner_telegram_id`/`business_id`), running-total print matches the DB sum
- Quota enforcement confirmed: temporarily lowering the limit correctly raises `QuotaExceeded` and blocks generation

**Side finding (FIXED in Part 6 via `_clean()` — junk values are now stripped before the prompt)**: the "Chai wala" test business has "Skip" as its literal phone and "Yes" as its hours — from earlier manual testing where `/skip` wasn't typed exactly, and neither field has format validation, so free text was saved and faithfully rendered (correctly — no fabrication). Cosmetic test-data issue, not a Part 2 bug. Optional: add phone format validation to `onboarding.py` if you want that tightened.

## Part 3 — Sandbox testing harness (done and verified live)

Standalone smoke-test harness — not wired to the pipeline yet, same "prove it alone first" pattern as Parts 1–2. Fully built and **verified against the real API**:

- `worker/tasks/sandbox.py` — `sandbox_test(files)` creates a **Daytona** sandbox (managed, container-based; chosen over local Docker/Fly Machines because you want a managed provider, and over plain Docker because generated sites are expected to include real backend code eventually, not just static HTML/CSS), writes the two generated files into it, starts a static file server inside as a background process (via a Daytona session with `run_async=True`), then runs Playwright (headless Chromium, on our side) against the sandbox's public preview URL. Always tears the sandbox down afterward, even on failure.
- Checks performed: page loads without error; zero console/page errors; **zero `<script>` tags** (regression check tying back to Part 2's "no JS" rule); `style.css` actually resolves; real title/heading content present (not a blank page); any `<img>` sources resolve; `tel:`/`mailto:` links are well-formed.
  *(Superseded by Part 6: now 9 checks — adds `internal_links_valid` and a minimum word count — run concurrently across all four generated pages, not just `index.html`.)*
- Returns a report shaped like the (currently unused) `site_versions.sandbox_report` column: `{"passed": bool, "checks": [...], "console_errors": [...]}` — Part 4 wires this into the DB for real; Part 3 just proves the shape standalone.
- A hardcoded **broken fixture** (bad `<script>`, thrown JS error, missing image) is built in, so the harness's failure detection can be proven without spending any API calls.
- `scripts/sandbox_test.py` — CLI: `--dir generated_output/<name>` (tests any of Part 2's existing output) or `--broken` (proves the harness actually fails bad output).

**Provider note**: originally built against E2B, but signing up required a card on file (same billing-friction pattern as Anthropic earlier), so — per your direction — swapped to **Daytona** instead (free tier, no card). Only `sandbox.py`'s sandbox-orchestration calls differ (Daytona's session-based background execution and `get_preview_link()` vs E2B's `commands.run(background=True)` and `get_host()`); the Playwright checks, report shape, and CLI are all unchanged. Verified the exact SDK surface (`AsyncDaytona`, `CreateSandboxFromSnapshotParams`, `fs.upload_file`, `process.create_session`/`execute_session_command`, `get_preview_link`) against the actually-installed `daytona` package (v0.204.0) before writing the code.

**Verified live**:
- `--broken` run: correctly caught all 3 deliberate defects — `no_console_errors` (thrown JS error), `no_script_tags` (1 found), `images_resolve` (404 on the missing image) — while `page_loads`, `css_loads`, `content_present`, `contact_links_valid` correctly passed (proves the harness isn't just failing everything).
- All three Part 2 outputs (chai-wala, plumber, salon) **passed all 7 checks**.
- Confirmed sandbox teardown: listed all Daytona sandboxes after all 4 runs (3 passing + 1 broken) — **0 remaining**, no leaks.

## Part 4a — Full create pipeline wiring (done and verified live)

Wires Parts 1–3 together for real: onboarding confirm → queue → generate → sandbox → deploy → notify. Split from the original "Part 4" so the core pipeline could be proven standalone before adding editing — see Part 4b below.

- `bot_api/services/queue.py` — Arq enqueue client (`enqueue_generation(business_id, trigger)`), reuses the same Upstash Redis instance already backing aiogram's FSM storage. `_job_id=f"generate:{business_id}:{trigger}"` dedups a double-tapped confirm button.
- `worker/worker.py` — Arq `WorkerSettings`, one function (`run_generation_pipeline`), `max_tries=1` (no whole-job auto-retry — fail cleanly once), `keep_result=60` (short on purpose, see bug note below). Run via `python -m arq worker.worker.WorkerSettings` from repo root.
- `worker/tasks/generate.py` — `run_generation_pipeline(ctx, business_id, trigger)`, the single orchestrating job: creates a `site_versions` row, calls Part 2's `build_site()`, then Part 3's `sandbox_test()`, then the new Cloudflare deploy step, updating `businesses.generation_status`/`site_versions.status` at every stage boundary. Any failure routes through one `_mark_failed()` helper (DB write + Telegram notify), no partial/broken state ever gets left live.
- `worker/tasks/deploy.py` — `deploy_to_cloudflare_pages(business, files)`: creates a **Cloudflare Pages** project per business (idempotent, reuses on re-deploy), uploads the files, creates a deployment, returns the stable `*.pages.dev` URL. Pure `httpx`, no DB/Telegram knowledge. *(Part 6: uploads all five files, and the returned hostname is now read from Cloudflare rather than derived from the project name — see Part 6's dead-link bug.)*
- `worker/tasks/notify.py` — Telegram success/failure messages, stage-differentiated copy (generation/quota/sandbox/deploy/unknown).
- `bot_api/bot/handlers/onboarding.py`'s `on_confirm` now actually calls `enqueue_generation(...)` instead of a placeholder message.
- Schema: `businesses.vercel_project_id` renamed to `cf_pages_project_name` (Alembic `0003`, applied to live Supabase).

**Provider note**: the original plan assumed Vercel's free Hobby tier for hosting generated sites. Vercel's Hobby ToS explicitly bans "any Deployment used for financial gain of anyone involved" and can disable Hobby projects without notice — since every generated site is for a business owner, that's client work, not a fit. Compared Netlify (ToS-friendly to commercial use, but its credit system caps the whole account at ~20 deploys/month — too tight) against **Cloudflare Pages** (no card, commercial-friendly, ~500 deploys/month, unlimited bandwidth) — chose Cloudflare. `bot_api` itself remains separately planned for Vercel (unrelated, still pending your Vercel account).

Cloudflare's asset-upload API (`upload-token` → `assets/upload` → `deployments`) isn't officially documented for plain REST use — reverse-engineered from community reports, then verified live against a real account. One correction that only surfaced by testing against the real API: `upload-token` is a **`GET`** request, not `POST` as every third-party writeup assumes.

**Bugs found and fixed during verification**:
- Arq's `_job_id` dedup blocks re-enqueueing for as long as the job's result is kept (`keep_result`, default 1 hour) — a failed run silently blocked its own retry for up to an hour, contradicting our own "try again in a bit" failure message. Fixed by setting `keep_result=60`.
- `TaskStop`/process-kill on a background worker did not reliably kill the underlying OS process on this machine, leaving a stray worker (with the old default settings) running invisibly alongside a fresh one — caused confusing duplicate job executions during testing. Worked around by explicitly enumerating and force-killing all `python.exe -m arq` processes by command line before each restart.

**Verified live**:
- Ran `sandbox_test()` + `deploy_to_cloudflare_pages()` directly against Part 2's real "chai-wala" output: all 7 sandbox checks passed, deployed successfully, `https://chai-wala.pages.dev` confirmed live and rendering the exact uploaded content.
- Full real end-to-end run through the actual queue/worker chain (not the direct-call test above): hit a **genuine, sustained Gemini API outage** (`503 UNAVAILABLE — high demand`, a known recurring issue on Google's free tier after model releases) for ~40 minutes across 7 attempts — each one correctly retried 3x internally, then failed cleanly with the right DB state (`site_versions.status='failed'`, real error recorded, `businesses.generation_status='failed'`) and the right Telegram failure message, proving the failure path end-to-end for real, repeatedly.
- Once Gemini recovered, the **full real chain succeeded**: `businesses.generation_status='live'`, `deployment_url='https://chai-wala.pages.dev'`, `cf_pages_project_name='chai-wala'`, `site_versions` row `status='live'`/`sandbox_status='passed'`/`deployed_url` populated — live URL fetched and confirmed rendering the real Gemini-generated content (title "Chai wala", real heading, CSS loading).

## Part 4b — Natural-language editing (done, parser verified live)

Split from the original "Part 4b" scope — contact-form backend deferred to Part 4c (needs `bot_api`'s real domain, still pending your Vercel account). This part lets an owner text the bot free text ("change my hours to 9-6") and have it update the live site. Built in two passes this session: a v1 (fixed operation set, no memory, refuses to write any content itself) that you real-tested in Telegram and found too rigid, then a v2 revision addressing exactly what you asked for.

**v1 — the core mechanism**:
- `bot_api/services/validation.py` — shared `EMAIL_RE` (moved out of `onboarding.py`, which imports it back) + `FIELD_LIMITS`/`THEMES` matching real DB column sizes.
- `bot_api/services/nl_edit.py` — `parse_edit_message(raw_message, business, context=None)` forces a structured operation call via OpenRouter (see provider-swap note below).
- `bot_api/services/edit_ops.py` — `is_business_busy()` (reuses `business.generation_status` as the concurrency guard — no new Redis lock needed) and `apply_edit_operation()` (validates + mutates `Business`/`Service` rows; rejects on any limit/format violation rather than silently clamping; deterministic case-insensitive service-name matching, never trusted blindly from the model).
- `bot_api/bot/handlers/edit.py`'s `catch_all_edit` rewritten: busy-check → parse → branch on operation → apply → log to `edit_log` (every outcome gets a row) → `enqueue_generation(business.id, trigger="edit")`.
- `bot_api/services/queue.py` bug fix: the `_job_id` dedup that correctly protects `"create"` from a double-tapped confirm button was also silently colliding across repeated `"edit"`s on the same business. Fixed by appending a random suffix to the job id whenever `trigger != "create"`.
- `scripts/nl_edit_test.py` — standalone parser test CLI: `--business-id <uuid> --message "..."`, `--canned` (5 built-in messages), or `--sequence <name>` (multi-turn scenarios threading real Redis-backed context, mirroring what the real handler does).

**v2 — flexibility revision, based on your real Telegram testing**: three gaps you found (no memory between messages, wouldn't write any content itself, couldn't add anything structurally new) fixed:
- **Conversation memory**: `bot_api/services/session.py` gained `get_edit_context`/`push_edit_turn` (Redis key `nl_edit_ctx:{business_id}`, last 3 turns, 10-min sliding TTL) — a bare "Yes" answering the bot's own previous question is now resolved correctly instead of misread as `not_an_edit`.
- **Content-authority split**: the prompt (in both `nl_edit.py` and `worker/codegen/prompts/site_builder.md`) now distinguishes *factual* fields (never invented) from *creative* fields (tagline/about — the model may now compose real marketing copy for a vague request like "add more detail," setting `drafted=true`) from *attributed third-party claims* (customer quotes/reviews/awards — never fabricated under any circumstance, even in creative fields) from *infeasible requests* (interactive/payment/multi-page features — explained plainly via `clarify`, never silently attempted).
- **Open-ended additions**: new `businesses.extra_instructions` column (Alembic `0004`) + `update_extra_instructions(instructions, mode)` operation — "add a testimonials section," "mention we offer free parking," etc. get folded into every future full-site regeneration via `site_builder.md`'s new "Extra instructions" section, reusing the entire existing generate→sandbox→deploy pipeline unchanged.
- **Draft-then-confirm**: model-*composed* tagline/about text (not the owner's own literal words) is held behind a lightweight yes/no confirmation (`pending_edit:{business_id}` Redis key, 600s TTL, deterministic affirmation matching — no extra LLM call needed) before ever touching the DB or triggering a redeploy. Every other edit type stays instant-apply.

**Verified live via real Telegram** (worker + polling both running, real messages sent to `@teko21bot`): a message sent *while a prior edit's pipeline was still running* was correctly rejected with the busy-guard message (`status=generating`), then correctly resent and processed — real proof the concurrency guard holds under an actual race, not just a simulated one. "Change navbar color, to green" correctly routed to the new `update_extra_instructions` operation (v1 would have flatly refused any color request) and the DB was updated correctly (`businesses.extra_instructions='navbar color: green'`), and the resulting regeneration deployed live successfully (complete HTML, sandbox passed).

**Known gap — partially addressed in Part 6, not re-verified.** Part 6's `_shared.md` now states `extra_instructions` are binding client instructions and explicitly includes colour requests for the navbar/header/buttons/background, and they also feed the `style_signature` so a new instruction forces a stylesheet rebuild rather than reusing a cached one. Whether "navbar color: green" now actually lands has **not** been re-tested — treat the gap below as open until it is.

Original finding: `extra_instructions` reliably *saves* and *doesn't break anything*, but the actual regeneration did **not** visibly apply "navbar color: green" — the redeployed CSS has no green anywhere, the nav still used its original color. The instruction reaches `site_builder.md`'s prompt correctly; the model (`nemotron-3-nano-30b-a3b:free`) just doesn't reliably act on it while also handling everything else in a full-site generation. So the "add anything by prompting" goal is only partially delivered today: structurally safe (never crashes, never fabricates), but not consistently *effective* for instructions that compete with the rest of the generation task for the model's attention. Worth a closer look (stronger prompt phrasing, or moving extra_instructions earlier/more prominently in the prompt, or trying the larger fallback model for edits that include extra_instructions) if this matters for real usage — not fixed as of this write-up.

Not yet exercised even once: the testimonials-without-a-real-quote guardrail, and an explicitly infeasible request (e.g. "add a booking calendar").

## Provider swap: Gemini → OpenRouter (Part 2 + Part 4b), mid-session

Gemini's free tier turned out to cap at **20 requests/day per project per model** — hit hard during Part 4b's real testing (both a 429 daily-cap and a separate 5-req/minute burst cap). You have an OpenRouter.ai account, so both Part 2 and Part 4b were moved there together (your call, confirmed via AskUserQuestion) rather than just one.

- New shared module **`bot_api/services/openrouter_client.py`** — two call shapes, not interchangeable: `call_forced_tool(prompt, tools)` (used by `bot_api/services/nl_edit.py` — short structured field values) and `call_plain_completion(prompt)` (used by `worker/codegen/builder.py` — long-form content; see the tool-call-length bug below for why generation specifically can't use forced tool-calling here). Verified live: OpenRouter's `tool_choice: "required"` (model picks among several tools) works the same way as forcing one specific named tool.
- **Model** *(generation half superseded by Part 6: `GENERATION_MODELS` now leads with `super-120b`, and `TOOL_MODELS` is a separate list so edit parsing still leads with nano)*: `nvidia/nemotron-3-nano-30b-a3b:free` is primary (not `nemotron-3-super-120b-a12b:free`, despite being the initially-planned "quality" choice) — verified live against the real site-generation prompt that nano answers in ~40s using ~500 completion tokens with zero internal-reasoning overhead and equally clean output, while super-120b took ~2 minutes and 8000+ tokens (85% of them pure reasoning) for a comparable result. super-120b is kept as the automatic fallback (used if nano 404s as "unavailable" — free-model rotation is real, already observed live mid-session with two other candidate models going paid-only).
- `google-genai` fully removed (from `pyproject.toml` and `requirements.txt`) — no SDK needed, OpenRouter is called via plain `httpx` (already a base dependency), matching `worker/tasks/deploy.py`'s existing raw-REST pattern.
- New config: `openrouter_api_key` in `Settings` + `.env`/`.env.example`. `gemini_api_key` stays present but unused.

**Real bugs found and fixed during this swap** (worth remembering if touching this code again):
- The model sometimes wrote the literal letter **`n`** in place of newline characters inside long JSON string arguments (e.g. `<title>...</title>n<h1>...`) — a real, observed quality defect in how these free models encode multi-line content inside tool-call JSON. First fix attempt: instructed the tool schema to request single-line output. That masked the symptom but not the underlying problem — see the next bug.
- **The real, underlying bug: OpenRouter's free tier caps individual tool-call ARGUMENT strings at ~1024 characters, regardless of model or `max_tokens`.** Confirmed live with `max_tokens=16000` and only 521 of it used — still cut off at exactly 1024 characters, `finish_reason: "tool_calls"` (a normal-looking stop, not `"length"`), on *both* candidate models independently. This silently produced genuinely broken, incomplete HTML documents (missing `</body>`/`</html>`, whole sections gone) that were saved and reported as "generated successfully" — **and Part 3's sandbox checks did not catch it**, because Chromium renders malformed/incomplete HTML permissively enough that `page_loads`/`content_present`/etc. all still passed. This means two earlier "verified live" claims in this file were premature — the salon and plumber outputs generated earlier today were truncated, and I did not actually inspect them closely enough to notice before reporting success. Real fix: `worker/codegen/builder.py` no longer uses forced tool-calling at all for generation — it now uses a plain text completion (`call_plain_completion`) with a delimiter format parsed in `worker/codegen/builder.py` (originally `===INDEX_HTML===`/`===STYLE_CSS===` via `RESPONSE_RE`; Part 6 generalised this to `===FILE: name===` markers parsed by `_parse_files()`, so any number of files can come back). Verified live this does not hit the same cap (an 8839-character plain completion came back complete, `finish_reason: "stop"`). `build_site()` also now explicitly checks the returned HTML ends with `</html>` and raises `GenerationFailed` if not, as a second line of defense. Part 4b's `nl_edit.py` keeps using forced tool-calling — its field values are short enough that the cap was never actually hit there.
- OpenRouter sometimes returns **HTTP 200 with the actual failure embedded in the JSON body** (`{"error": {...}}`) instead of a proper error status code — observed live with a transient `504 Upstream idle timeout exceeded` arriving this way. Fixed by checking for a body-level `error` key before assuming a 200 response is real.
- **`asyncio.wait_for` does not reliably enforce its timeout against an `asyncio.to_thread`-wrapped blocking call on this exact setup (Python 3.14.5 / Windows)** — confirmed with a minimal, network-free repro (`wait_for(to_thread(time.sleep(300)), timeout=5)` never fired). Real requests were hanging far past any configured httpx/asyncio timeout as a result. Fixed by bounding execution with `concurrent.futures.Future.result(timeout=...)` instead (an independent, more mature timeout mechanism) inside the thread, rather than relying on asyncio cancellation from outside it.
- Separately, and confusingly layered on top of the above while diagnosing it: **long-running Python processes launched via this coding agent's Bash tool on this machine did not reliably reflect real progress/timing** — the same code, launched via PowerShell instead, behaved exactly as expected (the `concurrent.futures` timeout fired at 5.0s on the nose). If a Python network call ever appears to hang again during development in this environment, try relaunching it via the PowerShell tool before assuming the code is broken.

**Verified live, for real this time** (PowerShell, content inspected byte-for-byte, not just token counts glanced at): `scripts/generate_site.py --sample plumber` and `--sample salon` both produce genuinely complete HTML documents (`</html>`-terminated, real multi-line formatting, no artifacts), and `scripts/sandbox_test.py` passes all 7 checks on both against real Daytona. `scripts/nl_edit_test.py --sequence yes-followup` confirmed the parser produces sensible results through OpenRouter, including correctly using conversation context. Not yet re-run since the swap: the full real end-to-end pipeline (Telegram → queue → worker → OpenRouter → Daytona → Cloudflare → notify) and the remaining Part 4b v2 scenarios (testimonials guardrail, infeasible-request handling). The already-live `chai-wala.pages.dev` deployment predates this swap (generated via Gemini during Part 4a) and is unaffected by any of these bugs.

## Part 6 — Rich multi-page generation + speed (done and verified live)

Triggered by real user testing: a short brief produced a *"very very very simple"* one-page site — a hero line, one paragraph, and **empty `Services` and `Photos` headings with nothing under them**.

**Root cause** (read off the real DB row, not guessed): the `website-maker` business has `services: []`, `media: []`, `hours: "So not include this"` and a two-sentence `about`, while `site_builder.md` said *"skip any section whose data is missing"* and *"never invent"*. So it faithfully transcribed four fields and stopped. The generator was a transcription engine, not a website builder.

**What changed**

- **Prompt split into three files** (`worker/codegen/prompts/`): `_shared.md` (a fixed **class contract** — `card-grid`, `faq-item`, `step-number`, `cta-band`, … — plus all fabrication/hygiene/technical rules), `stylesheet.md`, and `pages.md`. The contract is what lets CSS and HTML be written by *separate* model calls and still fit together. `site_builder.md` is deleted.
- **Four pages, not one**: `index.html`, `about.html`, `services.html`, `contact.html` + shared `style.css`, each page required to carry ≥400 words of real copy, a features block, a process section, an FAQ, and CTA bands.
- **Content-authority rules**: the model is now explicitly told to *write* headlines, body prose, benefits, FAQ answers and CTAs, while never inventing contact details, prices, hours, testimonials, awards, founding dates, statistics or staff names.
- **Parallel generation**: `build_site()` fires 3 concurrent calls via `asyncio.gather` (`style.css` | `index+about` | `services+contact`). Two page groups rather than four because the stylesheet is the largest artifact and sets the floor either way — four calls would cost two extra requests for no extra speed.
- **Model swap for generation**: `GENERATION_MODELS` now leads with `nemotron-3-super-120b-a12b:free` (157 tok/s vs ultra's 39). `TOOL_MODELS` is now a separate list, so Part 4b's edit parsing is unaffected. Measured live against the real prompt: ultra 408s/2599 words/37 CSS vars; lightning 134s but only 1 media query and 6 empty sections (rejected); super-120b 57s/1869 words. Splitting removes the fast model's disadvantage — each call gets its own full response budget instead of competing with four other files in one reply.
- **Stylesheet caching** (`businesses.style_css` + `style_signature`, migration `0005`): signature = hash of `theme` + `extra_instructions`. A content-only edit reuses the CSS and drops to **2 calls**; a theme or design-instruction change forces a rebuild. Only persisted after a fully successful build, so a stylesheet that failed the sandbox can't poison the next edit.
- **Junk-value stripping** (`_clean()` in `builder.py`): non-answers like `"Skip"`, `"none"`, `"n/a"`, `"-"` are dropped before the prompt ever sees them. This is the real fix for the long-standing "Chai wala has `phone: 'Skip'`" side finding noted under Part 2.
- **Parallel sandbox checks**: the new `_check_page()` in `worker/tasks/sandbox.py` returns per-page findings instead of mutating shared state, so all four pages are checked concurrently. Two new checks: **`internal_links_valid`** (a nav link to a page that wasn't generated is a live 404) and a **minimum word count** so a sparse page now *fails* instead of passing. 9 checks total.
- **Per-stage Telegram progress messages** (`notify_owner_progress` in `worker/tasks/notify.py`, called at each stage boundary in `generate.py`), plus an immediate "thinking about that" ack in `edit.py` before the parse call — you asked for this because builds felt silent.

**Results (measured, real pipeline runs)**

| | before | after |
|---|---|---|
| pipeline, content edit | 696s | **121s** |
| API calls per edit | 1 | 2 (3 on a design change) |
| pages | 1 sparse page | 4 pages: 402 / 363 / 553 / 274 words |
| empty section headings | Services + Photos | none |
| `<script>` tags | 4 (blocked the deploy) | 0 |
| sandbox checks | 8, sequential | 9, parallel, all passing |

Final state verified: `site_versions` v5 `status='live'`, `sandbox_status='passed'`, `error=NULL`, and `https://website-maker-6hf.pages.dev` fetched directly — all four pages plus the 7,711-byte stylesheet, zero script tags.

**Real bugs found and fixed in this part**

- **`document.write` copyright year.** Every generated page carried `&copy; <script>document.write(new Date().getFullYear())</script>`, which tripped `no_script_tags` and **correctly blocked a whole deploy** (`site_versions` v2 = failed). Fixed by passing `current_year` into the spec as data and forbidding JS-computed years.
- **Fabricated backstories — caused by my own prompt.** Requiring an "Our Story … 3-4 paragraphs" section on a business with no recorded history forced invention: super-120b wrote *"One evening, while experimenting with a Telegram bot for fun, the founder realized…"*; ultra wrote *"A freelance designer got a client-ready portfolio in six minutes."* An earlier "zero fabrication" claim in this session was **wrong** — the check only matched `since YYYY` / `N+ clients` / `N%` / `5-star` and missed narrative entirely. Fixed by dropping the mandated story section and explicitly banning origin stories, founder anecdotes and early-customer/tester claims. `about.html` now describes the business as it is today.
- **The dead-link bug (worst of the batch).** `worker/tasks/deploy.py` derived the hostname as `f"{project_name}.pages.dev"` on every re-deploy, but Cloudflare appends a suffix when a name is taken globally — the project `website-maker` is actually served at `website-maker-6hf.pages.dev`. So a re-deploy overwrote the owner's working link with a **419-byte parking page**, breaking the "the owner's link must never change" guarantee for *any* business whose slug collides globally. Fixed with a new `_get_project_subdomain()` that reads the real `subdomain` from Cloudflare instead of guessing (used in both the re-deploy path and `_ensure_project`'s already-exists branch). Only surfaced because both hostnames were fetched rather than trusting `status='live'`.
- **A false positive in a check I added.** The new class-coverage tripwire failed an otherwise-good build over one unstyled `faq` modifier sitting next to a styled `section` — cosmetically harmless. Narrowed to `_style_drift()`, which fails only on genuine mismatch: a **contract** class going unstyled, or more than 12 unstyled classes (meaning the stylesheet isn't the one those pages were written against). All three cases unit-verified. Worth remembering: a tripwire meant to catch bad output rejected good output.

**Known gaps**

- `contact.html` lands around 274 words, short of the 400 asked for. It clears the sandbox's 150-word floor and a contact page is inherently thin, but it is the weakest of the four.
- **Fabrication cannot be fully automated away.** The prompt bans invented narrative and there are now targeted greps for `founder` / `early tester` / `since YYYY` / placeholders, but generated copy still deserves a human read before a site is treated as client-ready.
- Each build is 3 API calls (2 for a content edit) against OpenRouter's free 50/day — roughly 16–25 builds per day. A one-time \$10 top-up raises this to 1,000/day.

## Not started yet (later parts, per the phased plan)

- **Part 4c — Shared contact-form backend**: `POST /api/{business_id}/contact` on `bot_api`, persists submissions, notifies the owner. Blocked on Part 1's `bot_api` Vercel deployment (needs a real domain for generated sites' `<form action>` to point at).
- **Part 5 — Hardening + production rollout**: retries, cost controls, logging, rate limiting, full smoke test

## Task tracker (this session)

| # | Task | Status |
|---|---|---|
| 1 | Scaffold Python project structure | ✅ done |
| 2 | Create Supabase project and apply DB schema | ✅ done |
| 3 | Build aiogram bot skeleton + Telegram webhook | ✅ done |
| 4 | Implement onboarding FSM | ✅ done |
| 5 | Implement multi-site commands and active-site session | ✅ done |
| 6 | Deploy Part 1 to Vercel and smoke test in real Telegram | ⏳ blocked on Vercel account |
| 7 | Apply `token_usage` table to Supabase | ✅ done |
| 8 | Install `anthropic` + scaffold `worker/` package | ✅ done |
| 9 | Build token quota system (`quota.py`) | ✅ done |
| 10 | Write codegen prompt template | ✅ done |
| 11 | Build codegen `builder.py` (Claude call + retry logic) | ✅ done |
| 12 | Write sample spec fixtures | ✅ done |
| 13 | Build `scripts/generate_site.py` CLI | ✅ done |
| 14 | Run Part 2 verification against real API | ✅ done (via Gemini) |
| 15 | Install `google-genai` + update `pyproject.toml` | ✅ done |
| 16 | Add `gemini_api_key` to config + `.env.example` | ✅ done |
| 17 | Rewrite `builder.py` for Gemini | ✅ done |
| 18 | Install `daytona` SDK + Playwright browser binary | ✅ done |
| 19 | Add `daytona_api_key` to config + `.env.example` | ✅ done |
| 20 | Build `worker/tasks/sandbox.py` (Daytona) | ✅ done |
| 21 | Build `scripts/sandbox_test.py` CLI | ✅ done |
| 22 | Run Part 3 verification against real API | ✅ done (via Daytona) |
| 23 | Add `cloudflare_api_token`/`cloudflare_account_id` to config + `.env.example` | ✅ done |
| 24 | Rename `businesses.vercel_project_id` → `cf_pages_project_name` (migration `0003`) | ✅ done |
| 25 | Build `bot_api/services/queue.py` (Arq enqueue client) | ✅ done |
| 26 | Build `worker/worker.py` (Arq `WorkerSettings`) | ✅ done |
| 27 | Build `worker/tasks/generate.py` (orchestrating pipeline job) | ✅ done |
| 28 | Build `worker/tasks/deploy.py` (Cloudflare Pages deploy) | ✅ done |
| 29 | Build `worker/tasks/notify.py` (Telegram success/failure messages) | ✅ done |
| 30 | Wire `on_confirm` to enqueue the real pipeline | ✅ done |
| 31 | Run Part 4a verification against real infra (real Telegram business, real Gemini, real Daytona, real Cloudflare) | ✅ done |
| 32 | Build `bot_api/services/validation.py` (shared field limits/regex) | ✅ done |
| 33 | Build `bot_api/services/nl_edit.py` (Gemini edit-intent parser) | ✅ done |
| 34 | Build `bot_api/services/edit_ops.py` (operation registry + validators) | ✅ done |
| 35 | Rewrite `catch_all_edit` in `bot_api/bot/handlers/edit.py` | ✅ done |
| 36 | Fix `queue.py` job-id collision across repeated edits | ✅ done |
| 37 | Build `scripts/nl_edit_test.py` CLI | ✅ done |
| 38 | Move `google-genai` to base deps + `requirements.txt` | ✅ done |
| 39 | Run Part 4b v1 verification against real API | ✅ done (via Gemini, before the OpenRouter swap) |
| 40 | Add conversation memory (`session.py` context helpers) | ✅ done |
| 41 | Rewrite content-authority rules in `nl_edit.py` + `site_builder.md` | ✅ done |
| 42 | Add `businesses.extra_instructions` (migration `0004`) + `update_extra_instructions` op | ✅ done |
| 43 | Add draft-then-confirm flow for composed creative copy | ✅ done |
| 44 | Swap Gemini → OpenRouter for Part 2 + Part 4b (`openrouter_client.py`) | ✅ done |
| 45 | Diagnose and fix the `asyncio.wait_for`/`to_thread` timeout bug | ✅ done |
| 46 | Run Part 2 + Part 4b verification against real OpenRouter API | ✅ done (partial — see provider-swap note; full Telegram walkthrough not yet re-run) |
| 47 | Add per-stage Telegram progress messages + "thinking" ack | ✅ done |
| 48 | Split prompt into `_shared.md` / `stylesheet.md` / `pages.md` with a class contract | ✅ done |
| 49 | Rewrite generator for 4 pages + generic `===FILE:===` parsing | ✅ done |
| 50 | Parallelise generation (3 concurrent calls) + swap generation model to super-120b | ✅ done |
| 51 | Add stylesheet caching (`style_css`/`style_signature`, migration `0005`) | ✅ done |
| 52 | Strip junk onboarding values (`_clean()`) — fixes the `phone: "Skip"` finding | ✅ done |
| 53 | Parallelise sandbox page checks + add `internal_links_valid` and word-count checks | ✅ done |
| 54 | Fix `document.write` copyright year (pass `current_year` as data) | ✅ done |
| 55 | Remove mandated "Our Story" section; ban invented backstories/anecdotes | ✅ done |
| 56 | Fix Cloudflare dead-link bug (`_get_project_subdomain`, stop guessing the hostname) | ✅ done |
| 57 | Narrow class-coverage check to `_style_drift()` after it false-positived | ✅ done |
| 58 | Run Part 6 verification against real infra (OpenRouter → Daytona → Cloudflare) | ✅ done (v5 live, 121s, 9/9 checks) |
| 59 | Re-test whether `extra_instructions` colour requests now apply | ⬜ not done |
| 60 | Test Part 4b v2 testimonials guardrail + infeasible-request handling | ⬜ not done |
