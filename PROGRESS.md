# Project Status

Full architecture/design doc: `C:\Users\Prahlad Singh\.claude\plans\let-continure-expressive-dream.md`
(Telegram bot → LLM generates a real website per business → tested in a sandbox → auto-deployed → link sent in chat. Built in phases; we are in **Part 1**.)

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

Waiting on you for:
1. **Supabase project** — you're creating this yourself (avoids the $10/mo extra-project cost on the existing org). Once created:
   - Run `db/schema.sql` in the Supabase SQL editor (or give me the project ID and I'll apply it via the Supabase MCP tools instead)
   - Create a **public** Storage bucket named `business-media`
   - Send me: the Postgres connection string (Project Settings → Database → Connection string, URI format) and the Project URL + `service_role` key (Project Settings → API)
2. **Telegram bot token** — from @BotFather, whenever you're ready to test for real
3. **Upstash Redis** — a free database's REST/TCP connection URL (needed for FSM state + active-site tracking)
4. **Vercel account** — for deployment (Task #6 below)

## Not started yet (later parts, per the phased plan)

- **Part 2 — Code generation harness**: LLM turns a business's saved spec into real frontend files (+ thin backend calls into a shared API)
- **Part 3 — Sandbox testing harness**: Fly.io Machines + Playwright smoke test before anything goes live
- **Part 4 — Hosting + full pipeline wiring**: direct Vercel Deployments API per business, Arq queue connecting bot → generate → sandbox → deploy → notify-in-chat; natural-language editing goes live here too
- **Part 5 — Hardening + production rollout**: retries, cost controls, logging, rate limiting, full smoke test

## Task tracker (this session)

| # | Task | Status |
|---|---|---|
| 1 | Scaffold Python project structure | ✅ done |
| 2 | Create Supabase project and apply DB schema | ⏳ waiting on you (see above) |
| 3 | Build aiogram bot skeleton + Telegram webhook | ✅ done |
| 4 | Implement onboarding FSM | ✅ done |
| 5 | Implement multi-site commands and active-site session | ✅ done |
| 6 | Deploy Part 1 to Vercel and smoke test in real Telegram | ⏳ blocked on #2 + bot token + Upstash + Vercel |
