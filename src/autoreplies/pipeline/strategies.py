"""Pipeline side-effect strategies.

Defines three Protocol types — SendStrategy, SlackStrategy, SupabaseStrategy —
that process_lead calls at each phase boundary. Production wires Live*
implementations; the harness (src/autoreplies/harness/) wires DraftSend/Noop*
without this module needing to know about them.

Live* classes are thin wrappers that raise NotImplementedError until the
corresponding PLAN.md phases are implemented. They exist now so the production
wiring is in place when those phases land — no further refactoring required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from autoreplies.parsers.base import ParsedLead


@dataclass
class SendResult:
    """Return value from SendStrategy.send_reply.

    sent_id is the Gmail message-id (LiveSend) or Airtable draft record ID
    (DraftSend). None when the send was skipped.
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
        # LiveSend may use these for logging; DraftSend writes them to the Drafts row.
        inquiry_record_id: str,
        gmail_message_id: str,
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
#
# All raise NotImplementedError until PLAN.md Phases 2-3 are implemented.
# GmailClient/SlackClient/SupabaseClient are constructed at call time in those
# phases — not held as constructor args here, since GmailClient is per-mailbox.
# ---------------------------------------------------------------------------


class LiveSend:
    """Production send: calls GmailClient.send_reply (Phase 2)."""

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
        reply_route: Literal["thread", "direct", "skipped"],
        skipped_reason: str | None,
        apartment_match_strategy: Literal["streeteasy_id", "address", "none"],
        apartment_match_confidence: int | None,
        template_source: Literal["agent", "pear_default"],
        llm_model: str,
        llm_latency_ms: int | None,
        notes: str,
    ) -> SendResult:
        raise NotImplementedError("Phase 2")


class LiveSlack:
    """Production Slack: calls SlackClient.post_lead / post_alert (Phase 3)."""

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
        raise NotImplementedError("Phase 3")

    def post_alert(self, *, summary: str, details: dict[str, Any]) -> str:
        raise NotImplementedError("Phase 3")


class LiveSupabase:
    """Production Supabase: calls SupabaseClient.upsert_inquiry (Phase 3)."""

    def upsert_inquiry(self, *, id: str, **fields: Any) -> dict[str, Any]:
        raise NotImplementedError("Phase 3")


def build_production_strategies() -> PipelineStrategies:
    """Construct the production strategy bundle.

    Live* stubs all raise NotImplementedError until PLAN.md Phases 2-3 are built.
    """
    return PipelineStrategies(
        send=LiveSend(),
        slack=LiveSlack(),
        supabase=LiveSupabase(),
    )
