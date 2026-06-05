"""Pipeline side-effect strategies.

Defines three Protocol types — SendStrategy, SlackStrategy, SupabaseStrategy —
that process_lead calls at each phase boundary. Production wires Live*
implementations; the harness (src/autoreplies/harness/) wires DraftSend/Noop*
without this module needing to know about them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from autoreplies.config import get_settings
from autoreplies.parsers.base import ParsedLead
from autoreplies.utils.humanization import compute_send_at
from autoreplies.workers.send_job import send_reply_job


@dataclass
class SendResult:
    """Return value from SendStrategy.send_reply.

    sent_id is the RQ job-id (LiveSend) or Airtable draft record ID (DraftSend).
    None when the send was skipped.
    """

    sent_id: str | None


class SendStrategy(Protocol):
    """Handles outbound reply delivery."""

    def send_reply(
        self,
        *,
        to: str,
        subject: str,
        plaintext_body: str,
        html_body: str,
        in_reply_to_message_id: str | None,
        thread_id: str | None,
        agent: dict[str, Any],
        parsed: ParsedLead,
        # Context carried from Phase A orchestration.
        # LiveSend uses mailbox_email to build the GmailClient inside the RQ job;
        # DraftSend writes it to the Drafts row's Sender field.
        inquiry_record_id: str,
        gmail_message_id: str,
        mailbox_email: str,
        reply_route: Literal["thread", "direct", "skipped"],
        skipped_reason: str | None,
        apartment_match_strategy: Literal["streeteasy_id", "address", "none"],
        apartment_match_confidence: int | None,
        template_source: Literal["agent", "pear_default"],
        llm_model: str,
        llm_latency_ms: int | None,
        notes: str,
    ) -> SendResult: ...


class SlackStrategy(Protocol):
    """Handles Slack notifications."""

    def post_lead(
        self,
        *,
        source: str,
        agent_name: str,
        agent_email: str,
        prospect_name: str | None,
        prospect_email: str | None,
        prospect_phone: str | None,
        apartment_address: str | None,
        apartment_match_confidence: int | None,
        message_excerpt: str | None,
        airtable_record_id: str,
        gmail_thread_url: str,
    ) -> str: ...

    def post_alert(self, *, summary: str, details: dict[str, Any]) -> str: ...


class SupabaseStrategy(Protocol):
    """Handles Supabase writes."""

    def upsert_inquiry(self, *, id: str, **fields: Any) -> dict[str, Any]: ...


@dataclass
class PipelineStrategies:
    """Bundle of all three strategies injected into process_lead."""

    send: SendStrategy
    slack: SlackStrategy
    supabase: SupabaseStrategy


# ---------------------------------------------------------------------------
# Production implementations (Live*)
# ---------------------------------------------------------------------------


class LiveSend:
    """Production send: enqueues a delayed RQ job via compute_send_at."""

    def __init__(self, queue: Any) -> None:
        """queue: an rq.Queue instance connected to Redis."""
        self._queue = queue

    def send_reply(
        self,
        *,
        to: str,
        subject: str,
        plaintext_body: str,
        html_body: str,
        in_reply_to_message_id: str | None,
        thread_id: str | None,
        agent: dict[str, Any],
        parsed: ParsedLead,
        inquiry_record_id: str,
        gmail_message_id: str,
        mailbox_email: str,
        reply_route: Literal["thread", "direct", "skipped"],
        skipped_reason: str | None,
        apartment_match_strategy: Literal["streeteasy_id", "address", "none"],
        apartment_match_confidence: int | None,
        template_source: Literal["agent", "pear_default"],
        llm_model: str,
        llm_latency_ms: int | None,
        notes: str,
    ) -> SendResult:
        if reply_route == "skipped":
            return SendResult(sent_id=None)

        settings = get_settings()
        send_at = compute_send_at(
            datetime.now(tz=UTC),
            tz_name=settings.humanization_timezone,
            working_hours=(
                settings.humanization_working_hours_start,
                settings.humanization_working_hours_end,
            ),
            within_jitter_seconds=(
                settings.humanization_within_jitter_min_sec,
                settings.humanization_within_jitter_max_sec,
            ),
            out_of_hours_jitter_seconds=(
                settings.humanization_out_jitter_min_sec,
                settings.humanization_out_jitter_max_sec,
            ),
        )

        job = self._queue.enqueue_at(
            send_at,
            send_reply_job,
            mailbox_email=mailbox_email,
            inquiry_record_id=inquiry_record_id,
            to=to,
            subject=subject,
            plaintext_body=plaintext_body,
            html_body=html_body,
            in_reply_to_message_id=in_reply_to_message_id,
            thread_id=thread_id,
        )
        return SendResult(sent_id=job.id)


class LiveSlack:
    """Production Slack: posts Block Kit lead notifications."""

    def __init__(self, client: Any) -> None:
        """client: a services.slack.SlackClient instance."""
        self._client = client

    def post_lead(
        self,
        *,
        source: str,
        agent_name: str,
        agent_email: str,
        prospect_name: str | None,
        prospect_email: str | None,
        prospect_phone: str | None,
        apartment_address: str | None,
        apartment_match_confidence: int | None,
        message_excerpt: str | None,
        airtable_record_id: str,
        gmail_thread_url: str,
    ) -> str:
        return self._client.post_lead(
            source=source,
            agent_name=agent_name,
            agent_email=agent_email,
            prospect_name=prospect_name,
            prospect_email=prospect_email,
            prospect_phone=prospect_phone,
            apartment_address=apartment_address,
            apartment_match_confidence=apartment_match_confidence,
            message_excerpt=message_excerpt,
            airtable_record_id=airtable_record_id,
            gmail_thread_url=gmail_thread_url,
        )

    def post_alert(self, *, summary: str, details: dict[str, Any]) -> str:
        return self._client.post_alert(summary=summary, details=details)


class LiveSupabase:
    """Production Supabase: upserts inquiry rows."""

    def __init__(self, client: Any) -> None:
        """client: a services.supabase.SupabaseClient instance."""
        self._client = client

    def upsert_inquiry(self, *, id: str, **fields: Any) -> dict[str, Any]:
        return self._client.upsert_inquiry(id=id, **fields)


def build_production_strategies(
    *, queue: Any, slack_client: Any, supabase_client: Any
) -> PipelineStrategies:
    """Construct the production strategy bundle with real client instances."""
    return PipelineStrategies(
        send=LiveSend(queue=queue),
        slack=LiveSlack(client=slack_client),
        supabase=LiveSupabase(client=supabase_client),
    )
