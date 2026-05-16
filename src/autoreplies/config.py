"""Environment-driven configuration via pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Loaded from .env / process env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Service
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # Admin auth
    admin_token: str = Field(default="dev-token-change-me", min_length=16)

    # Pub/Sub
    pubsub_audience: str = "https://autoreplies.pearnyc.com/pubsub/inbox"
    pubsub_service_account_email: str = ""

    # Google
    google_application_credentials: str = "/etc/pear-autoreply/sa.json"

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # Airtable
    airtable_token: str = ""
    airtable_base_id: str = "appwPKlnV6YtbIjWz"
    airtable_staging_base_id: str = ""
    airtable_test_base_id: str = ""

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # Slack
    slack_bot_token: str = ""
    slack_channel: str = "#platform-leads"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    dedup_ttl_days: int = 30

    # Send timing
    quiet_hours_tz: str = "America/New_York"
    quiet_hours_start: int = 23
    quiet_hours_end: int = 7
    send_jitter_min_seconds: int = 120
    send_jitter_max_seconds: int = 300

    # Harness
    harness_poll_interval_seconds: int = 60
    harness_bootstrap_lookback_seconds: int = 60
    harness_state_path: str = "/var/lib/pear-autoreply/harness.sqlite"

    @property
    def active_airtable_base_id(self) -> str:
        """Use the staging base when configured (dev/staging only)."""
        if self.app_env != "production" and self.airtable_staging_base_id:
            return self.airtable_staging_base_id
        return self.airtable_base_id

    @property
    def harness_airtable_base_id(self) -> str:
        """Require the test base ID; raises loudly if not configured."""
        if not self.airtable_test_base_id:
            raise RuntimeError("AIRTABLE_TEST_BASE_ID must be set when running in harness mode.")
        return self.airtable_test_base_id


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Use this everywhere instead of instantiating directly."""
    return Settings()
