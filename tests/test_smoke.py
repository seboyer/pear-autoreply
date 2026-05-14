"""Smoke tests — package imports cleanly and the FastAPI app constructs.

These run in CI on every PR. They catch the dumb stuff: typos in module
paths, circular imports, missing __init__.py, broken settings defaults.
"""

import importlib

import pytest


@pytest.mark.parametrize(
    "module_path",
    [
        "autoreplies",
        "autoreplies.main",
        "autoreplies.config",
        "autoreplies.logging_config",
        "autoreplies.deps",
        "autoreplies.routes.health",
        "autoreplies.routes.pubsub",
        "autoreplies.routes.admin",
        "autoreplies.services.gmail",
        "autoreplies.services.airtable",
        "autoreplies.services.supabase",
        "autoreplies.services.slack",
        "autoreplies.services.llm",
        "autoreplies.parsers",
        "autoreplies.parsers.base",
        "autoreplies.parsers.streeteasy",
        "autoreplies.parsers.zillow",
        "autoreplies.pipeline.process_lead",
        "autoreplies.pipeline.strategies",
        "autoreplies.utils.coerce",
        "autoreplies.workers.worker",
        "autoreplies.workers.scheduler",
    ],
)
def test_module_imports(module_path: str) -> None:
    importlib.import_module(module_path)


def test_settings_load_with_test_env() -> None:
    from autoreplies.config import get_settings

    settings = get_settings()
    assert settings.app_env in {"development", "staging", "production"}
    assert settings.admin_token  # something is set
    assert settings.anthropic_model.startswith("claude-")


def test_app_constructs_with_routes() -> None:
    from autoreplies.main import app

    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/healthz" in paths
    assert "/pubsub/inbox" in paths
    assert "/admin/replay/{message_id}" in paths
