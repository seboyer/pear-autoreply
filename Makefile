.PHONY: help install dev test lint format typecheck up down logs shell clean \
        schema-regen harness-watch harness-backfill harness-replay harness-diff harness-stats

# Use a non-dot venv directory. macOS auto-applies the UF_HIDDEN flag to
# dot-prefixed directories inside iCloud-synced locations (~/Documents,
# ~/Desktop), and CPython's site.py skips hidden .pth files — which silently
# breaks editable installs. See uv#16977.
UV_PROJECT_ENVIRONMENT ?= venv
export UV_PROJECT_ENVIRONMENT

help:
	@echo "Pear Autoreplies — make targets"
	@echo ""
	@echo "  install     install runtime + dev deps with uv"
	@echo "  dev         run the FastAPI app locally with hot reload"
	@echo "  worker      run an RQ worker locally"
	@echo "  test        run pytest"
	@echo "  lint        ruff check"
	@echo "  format      ruff format"
	@echo "  typecheck   mypy"
	@echo "  up          docker compose up -d"
	@echo "  down        docker compose down"
	@echo "  logs        tail compose logs"
	@echo "  shell       shell into the web container"
	@echo "  clean       remove caches"
	@echo ""
	@echo "  schema-regen ARGS=...           regenerate airtable_schema.py (e.g. ARGS='--base TEST')"
	@echo "  harness-watch                   start harness-poller in the background"
	@echo "  harness-backfill SINCE=YYYY-MM-DD [MAILBOX=...] [LIMIT=N]"
	@echo "  harness-replay MID=<msg-id> MAILBOX=<email>"
	@echo "  harness-diff SINCE=YYYY-MM-DD [OUT=<file>]"
	@echo "  harness-stats SINCE=YYYY-MM-DD"

install:
	uv sync --extra dev

dev:
	uv run uvicorn autoreplies.main:app --reload --host 0.0.0.0 --port 8000

worker:
	uv run python -m autoreplies.workers.worker

test:
	uv run pytest

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

typecheck:
	uv run mypy

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

shell:
	docker compose exec web /bin/bash

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# ── Schema ────────────────────────────────────────────────────────────────────

schema-regen:
	uv run python scripts/generate_airtable_schema.py $(ARGS)

# ── Harness ───────────────────────────────────────────────────────────────────

harness-watch:
	docker compose up -d harness-poller

harness-backfill:
	docker compose run --rm harness-poller python -m autoreplies.harness backfill \
		--since $(SINCE) \
		$(if $(MAILBOX),--mailbox $(MAILBOX),) \
		$(if $(LIMIT),--limit $(LIMIT),)

harness-replay:
	docker compose run --rm harness-poller python -m autoreplies.harness replay \
		$(MID) --mailbox $(MAILBOX)

harness-diff:
	docker compose run --rm harness-poller python -m autoreplies.harness diff \
		--since $(SINCE) \
		$(if $(OUT),--out $(OUT),)

harness-stats:
	docker compose run --rm harness-poller python -m autoreplies.harness stats \
		--since $(SINCE)
