"""Pytest configuration shared across the suite."""

import os

import pytest
from fastapi.testclient import TestClient

# Force a deterministic admin token for tests, regardless of the local .env.
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-1234567890abcdef")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")  # test DB


@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient with a fresh app instance.

    Importing `app` after env vars are set ensures Settings picks up the test values.
    """
    from autoreplies.main import app

    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-token-1234567890abcdef"}
