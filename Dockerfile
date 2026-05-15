# syntax=docker/dockerfile:1.7
# Multi-stage Python 3.12 build using uv for fast, reproducible installs.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.5.0 /uv /usr/local/bin/uv

WORKDIR /app

# ---- deps stage: install runtime deps only ----
FROM base AS deps

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv venv /opt/venv \
 && uv pip install --no-deps --python /opt/venv/bin/python -e .

# Resolve and install all transitive deps based on pyproject.
RUN uv pip install --python /opt/venv/bin/python \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" \
    "pydantic>=2.9" "pydantic-settings>=2.6" \
    "rq>=1.16" "redis>=5.0" \
    "google-auth>=2.35" "google-api-python-client>=2.149" \
    "pyairtable>=3.0" "supabase>=2.9" "slack-sdk>=3.33" "anthropic>=0.39" \
    "beautifulsoup4>=4.12" "lxml>=5.3" "rapidfuzz>=3.10" "httpx>=0.27"

# ---- runtime stage ----
FROM base AS runtime

# Non-root user
RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=deps /opt/venv /opt/venv
COPY --chown=app:app src ./src
COPY --chown=app:app pyproject.toml ./

USER app

# Default to running the web server. Compose overrides this for worker / scheduler.
EXPOSE 8000
CMD ["uvicorn", "autoreplies.main:app", "--host", "0.0.0.0", "--port", "8000"]
