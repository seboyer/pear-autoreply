"""Slack notifier via slack-sdk Block Kit."""

from typing import Any

from slack_sdk import WebClient


class SlackClient:
    def __init__(self, bot_token: str, channel: str) -> None:
        self._client = WebClient(token=bot_token)
        self.channel = channel

    def post_lead(
        self,
        *,
        source: str,  # "StreetEasy" | "Zillow"
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
        match_badge = (
            f":white_check_mark: {apartment_match_confidence}%"
            if apartment_match_confidence
            else ":x: No match"
        )

        fields: list[dict[str, Any]] = [
            {"type": "mrkdwn", "text": f"*Agent*\n{agent_name} — {agent_email}"},
            {"type": "mrkdwn", "text": f"*Source*\n{source}"},
        ]
        if prospect_name or prospect_email:
            contact = " | ".join(p for p in [prospect_name, prospect_email] if p)
            fields.append({"type": "mrkdwn", "text": f"*Prospect*\n{contact}"})
        if prospect_phone:
            fields.append({"type": "mrkdwn", "text": f"*Phone*\n{prospect_phone}"})
        if apartment_address:
            fields.append(
                {"type": "mrkdwn", "text": f"*Apartment*\n{apartment_address} — {match_badge}"}
            )

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":envelope: *New lead from {source}*",
                },
            },
            {"type": "section", "fields": fields},
        ]

        if message_excerpt:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Message*\n{message_excerpt}"},
                }
            )

        links: list[str] = []
        if airtable_record_id:
            links.append(
                f"<https://airtable.com/appwPKlnV6YtbIjWz/tbl4FU5cnMQVhQB0e/{airtable_record_id}|Airtable record>"
            )
        if gmail_thread_url:
            links.append(f"<{gmail_thread_url}|Gmail thread>")
        if links:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": " | ".join(links)},
                }
            )

        resp = self._client.chat_postMessage(channel=self.channel, blocks=blocks)
        return resp["ts"]

    def post_alert(self, *, summary: str, details: dict[str, Any]) -> str:
        """Post a yellow-flag alert (parser failed, agent not found, etc.)."""
        detail_lines = "\n".join(f"• *{k}*: {v}" for k, v in details.items())
        text = (
            f":warning: *{summary}*\n{detail_lines}" if detail_lines else f":warning: *{summary}*"
        )
        resp = self._client.chat_postMessage(channel=self.channel, text=text)
        return resp["ts"]
