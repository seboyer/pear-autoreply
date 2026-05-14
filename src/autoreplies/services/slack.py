"""Slack notifier (stub).

Real implementation lands in Phase 3. Uses slack-sdk.
Single channel target (`#platform-leads`); no @-mentions per PLAN.md § 7.
"""

from typing import Any


class SlackClient:
    def __init__(self, bot_token: str, channel: str) -> None:
        self.bot_token = bot_token
        self.channel = channel

    def post_lead(
        self,
        *,
        source: str,                          # "StreetEasy" | "Zillow"
        agent_name: str,
        agent_email: str,
        prospect_name: str | None,
        prospect_email: str | None,
        prospect_phone: str | None,
        apartment_address: str | None,
        apartment_match_confidence: int | None,  # 0-100, or None
        message_excerpt: str | None,
        airtable_record_id: str,
        gmail_thread_url: str,
    ) -> str:
        """Post the Block Kit lead notification. Returns the posted Slack ts."""
        raise NotImplementedError("Phase 3")

    def post_alert(self, *, summary: str, details: dict[str, Any]) -> str:
        """Post a yellow-flag alert (parser failed, agent not found, signature missing, etc.)."""
        raise NotImplementedError("Phase 3")
