# Pear Autoreplies

Real-time pipeline that ingests StreetEasy and Zillow rental-platform lead emails, sends an auto-reply, and records the lead in Airtable + Supabase + Slack.

## Two products in this repo

This repository contains two related but distinct products that share parsers, matchers, and template-fill logic by design:

1. **Production autoreply pipeline** — defined by [`PLAN.md`](./PLAN.md). The system that will actually send replies and write production data. Currently Phase 0 scaffolding.
2. **Testing harness** — defined by [`TESTING_HARNESS_PLAN.md`](./TESTING_HARNESS_PLAN.md), with build instructions in [`HARNESS_BUILD_BRIEF.md`](./HARNESS_BUILD_BRIEF.md). A side-car that polls the same Gmail mailboxes and materializes every would-have-sent reply as a row in a **test** Airtable base — sending nothing, posting nothing. Runs for a validation window before production cuts over.

Production code (everything in `src/autoreplies/` outside `src/autoreplies/harness/`) **never** imports the harness. This is enforced by [`tests/test_distinctness.py`](./tests/test_distinctness.py).

See [`FALLBACK_TEMPLATE.md`](./FALLBACK_TEMPLATE.md) for the generic Pear-wide reply template, and [`CLAUDE.md`](./CLAUDE.md) for project conventions and hard rules.

## Stack

- **Python 3.12** managed with [uv](https://github.com/astral-sh/uv)
- **FastAPI** for the Pub/Sub webhook + admin endpoints
- **RQ + Redis** for the worker queue + idempotency state
- **Caddy** as TLS-terminating reverse proxy (auto Let's Encrypt)
- **Docker Compose** for both local dev and the production droplet
- Anthropic Claude Haiku 4.5 for templated reply slot-fill

## Repository layout

```
src/autoreplies/      Python package — all production code
  main.py             FastAPI app
  config.py           pydantic-settings (env-driven)
  deps.py             FastAPI dependencies (bearer-token auth)
  routes/             HTTP endpoints (health, pubsub, admin)
  services/           Outbound clients (Gmail, Airtable, Supabase, Slack, LLM)
  parsers/            Source-specific email parsers (StreetEasy, Zillow)
  pipeline/           Worker pipeline orchestration
  utils/              Shared helpers (coercion, etc.)
  workers/            RQ worker + scheduler entrypoints

tests/                Pytest suite
legacy/               Reference: original Zapier Supabase script
fixtures/             Local email fixtures (gitignored — see fixtures/README.md)
```

## Local development

```bash
# One-time setup
cp .env.example .env
# Fill in real secrets in .env (see comments)
make install

# Run the API with hot reload
make dev

# In another terminal, start a worker
make worker

# Run tests
make test
```

The healthcheck is at <http://localhost:8000/healthz>.

### macOS + iCloud note

This project's venv lives at `venv/` (not `.venv/`). On macOS, iCloud Drive auto-applies the `UF_HIDDEN` flag to dot-prefixed directories inside `~/Documents` and `~/Desktop`, and CPython skips hidden `.pth` files — silently breaking editable installs (see [uv#16977](https://github.com/astral-sh/uv/issues/16977)).

`make` targets handle this via `UV_PROJECT_ENVIRONMENT=venv` set in the Makefile. For direct `uv run` / `uv sync` invocations from your shell, add this once to your `~/.zshrc` (or `.envrc` if you use direnv):

```bash
export UV_PROJECT_ENVIRONMENT=venv
```

Without it, `uv run` will create a fresh `.venv/` that ends up hidden and unusable.

## Docker

```bash
make up        # build + start web, worker, scheduler, redis
make logs      # tail logs
make down      # stop everything
```

## Deployment

Target: a single DigitalOcean droplet running this `docker-compose.yml`. Caddy auto-provisions Let's Encrypt certificates for the `/pubsub/inbox` endpoint (required by Pub/Sub push). See `PLAN.md` § "Deployment" for full provisioning notes.

## Testing harness

The harness polls the same Gmail mailboxes as production and writes every would-have-sent reply as a Drafts row in a separate test Airtable base. Nothing is sent; no production data is touched. See [`TESTING_HARNESS_PLAN.md`](./TESTING_HARNESS_PLAN.md) for the full design.

### Prerequisites

Add these to your `.env` (see `.env.example` for the full list):

```
AIRTABLE_TEST_BASE_ID=appmSm1FyerysvtcX
HARNESS_STATE_PATH=/var/lib/pear-autoreply/harness.sqlite
```

### Quickstart

```bash
# Start the poller (runs in the background, polls every 60 s)
make harness-watch

# Backfill all leads since a date (one-shot)
make harness-backfill SINCE=2026-05-01

# Backfill a single mailbox with a cap
make harness-backfill SINCE=2026-05-01 MAILBOX=agent@pearnyc.com LIMIT=20

# Re-run one message (bypasses dedup — useful for debugging)
make harness-replay MID=<gmail-message-id> MAILBOX=agent@pearnyc.com

# Print aggregate stats over the test base
make harness-stats SINCE=2026-05-01

# Write a CSV diff of prod vs test Inquiries
make harness-diff SINCE=2026-05-01 OUT=/tmp/diff.csv

# Regenerate airtable_schema.py after adding a field to CURATED
make schema-regen ARGS="--base TEST"
```

## Phase status

Currently in **Phase 0 — scaffolding**. None of the service clients or parsers are implemented yet; they're stubs that raise `NotImplementedError`. See `PLAN.md` § "Implementation phases" for what comes next. The testing harness build is sequenced in `HARNESS_BUILD_BRIEF.md` (phases H1–H7) and runs in parallel to PLAN.md Phases 1–4 — it must complete and soak before PLAN.md Phase 5 cutover.
