# Project Status

Full architecture/design doc: `C:\Users\Prahlad Singh\.claude\plans\let-continure-expressive-dream.md`
(Telegram bot → LLM generates a real website per business → tested in a sandbox → auto-deployed → link sent in chat. Built in phases; **Parts 1, 2, 3, and 4a are done and verified. Part 4b is built but not yet verified live — blocked on Gemini's free-tier daily quota, exhausted by today's testing.**)

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

**Provider note**: built against Claude Sonnet 5, but your Anthropic account had no credit balance, so codegen currently calls **Gemini** instead (free tier, no billing needed) — only `builder.py`'s API-calling internals differ; everything else in Part 2 is provider-agnostic and unchanged. Using the `gemini-flash-latest` alias (currently resolves to `gemini-3.7-flash` — `gemini-2.5-flash` itself turned out to be retired for new accounts). `build_site()`'s interface is unchanged, so switching back to Claude later is a small, contained change.

**Verified live** — ran against the real "Chai wala" business and both sample specs:
- Output is clean, semantic, professional HTML/CSS; themes are visibly distinct (modern = clean/light, bold = dark with a vivid accent)
- No fabricated content — every generation faithfully reflects only the data actually in the spec
- Zero `<form>` elements or `fetch()` calls anywhere — contact info renders as static `tel:`/`mailto:` links only, confirmed via grep across all output
- `token_usage` row written correctly (794 in + 3654 out, `gemini-3.7-flash`, correct `owner_telegram_id`/`business_id`), running-total print matches the DB sum
- Quota enforcement confirmed: temporarily lowering the limit correctly raises `QuotaExceeded` and blocks generation

**Side finding (not fixed, your call)**: the "Chai wala" test business has "Skip" as its literal phone and "Yes" as its hours — from earlier manual testing where `/skip` wasn't typed exactly, and neither field has format validation, so free text was saved and faithfully rendered (correctly — no fabrication). Cosmetic test-data issue, not a Part 2 bug. Optional: add phone format validation to `onboarding.py` if you want that tightened.

## Part 3 — Sandbox testing harness (done and verified live)

Standalone smoke-test harness — not wired to the pipeline yet, same "prove it alone first" pattern as Parts 1–2. Fully built and **verified against the real API**:

- `worker/tasks/sandbox.py` — `sandbox_test(files)` creates a **Daytona** sandbox (managed, container-based; chosen over local Docker/Fly Machines because you want a managed provider, and over plain Docker because generated sites are expected to include real backend code eventually, not just static HTML/CSS), writes the two generated files into it, starts a static file server inside as a background process (via a Daytona session with `run_async=True`), then runs Playwright (headless Chromium, on our side) against the sandbox's public preview URL. Always tears the sandbox down afterward, even on failure.
- Checks performed: page loads without error; zero console/page errors; **zero `<script>` tags** (regression check tying back to Part 2's "no JS" rule); `style.css` actually resolves; real title/heading content present (not a blank page); any `<img>` sources resolve; `tel:`/`mailto:` links are well-formed.
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
- `worker/tasks/deploy.py` — `deploy_to_cloudflare_pages(business, files)`: creates a **Cloudflare Pages** project per business (idempotent, reuses on re-deploy), uploads the two files, creates a deployment, returns the stable `*.pages.dev` URL. Pure `httpx`, no DB/Telegram knowledge.
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

## Part 4b — Natural-language editing (built, verification blocked on Gemini daily quota)

Split from the original "Part 4b" scope — contact-form backend deferred to Part 4c (needs `bot_api`'s real domain, still pending your Vercel account). This part lets an owner text the bot free text ("change my hours to 9-6") and have it update the live site.

- `bot_api/services/validation.py` — shared `EMAIL_RE` (moved out of `onboarding.py`, which now imports it back) + `FIELD_LIMITS`/`THEMES` matching real DB column sizes.
- `bot_api/services/nl_edit.py` — `parse_edit_message(raw_message, business)`, reuses the exact Gemini forced-function-call pattern proven in Part 2's `builder.py` (not Claude — avoids an unverified provider path). Six functions: `update_business_info` (name/tagline/about/phone/email/address/theme/hours, only fields actually mentioned), `add_service`, `update_service`, `remove_service`, `clarify` (genuine ambiguity or out-of-scope request), `not_an_edit` (not a change request at all).
- `bot_api/services/edit_ops.py` — `is_business_busy()` (reuses `business.generation_status` as the concurrency guard — no new Redis lock needed) and `apply_edit_operation()` (validates + mutates `Business`/`Service` rows; rejects on any limit/format violation rather than silently clamping; deterministic case-insensitive service-name matching, never trusted blindly from the model).
- `bot_api/bot/handlers/edit.py`'s `catch_all_edit` rewritten: busy-check → parse → branch on `clarify`/`not_an_edit`/real op → apply → log to `edit_log` (every outcome gets a row) → `enqueue_generation(business.id, trigger="edit")`.
- `bot_api/services/queue.py` bug fix: the `_job_id` dedup that correctly protects `"create"` from a double-tapped confirm button was also silently colliding across repeated `"edit"`s on the same business (a second edit sent within the 60s `keep_result` window after the first one finished would silently no-op). Fixed by appending a random suffix to the job id whenever `trigger != "create"` — the real double-submit guard for edits is the `is_business_busy` check, not an accidental side effect of the id scheme.
- `scripts/nl_edit_test.py` — standalone parser test CLI (`--business-id <uuid> --message "..."` or `--canned` for 5 built-in test messages), mirrors `scripts/generate_site.py`'s role for Part 2.
- `google-genai` moved from `pyproject.toml`'s `worker`-only extra into the base `dependencies` (this code runs inside `bot_api`, not the worker) and added to `requirements.txt` (the separate list Vercel's Python runtime actually installs from — was missing entirely, would've broken silently in production).

**All code import-checks cleanly. Live verification is blocked**: `scripts/nl_edit_test.py --canned` against the real "chai-wala" business hit `429 RESOURCE_EXHAUSTED` on every message — Gemini's free tier caps `gemini-3.7-flash` at **20 requests/day per project**, and today's extensive testing (Part 2, Part 4a's 8 pipeline attempts each retrying internally up to 3x, plus this test) exhausted it. This is a hard daily cap, not something retries fix — real verification needs to wait for the quota to reset (typically a rolling/midnight-Pacific daily window) and then run `scripts/nl_edit_test.py --canned` first, followed by the real-Telegram verification plan already detailed in the plan file's Part 4b section.

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
| 39 | Run Part 4b verification against real API | ⏳ blocked on Gemini's free-tier daily quota (exhausted today, resets on its own daily cycle) |
