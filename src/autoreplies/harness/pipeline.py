"""Harness-mode pipeline strategies and factory.

DraftSend, NoopSlack, NoopSupabase are the harness implementations of the
three strategy protocols. They are wired by build_harness_strategies(); the
production Live* remain in pipeline/strategies.py and are never imported here.

parser_used taxonomy: ParsedLead.parser_used holds "streeteasy"/"zillow"/"llm_fallback".
The Drafts table uses "regex"/"llm_fallback". The mapping lives here — not in
ParsedLead and not in AirtableClient.create_draft.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from autoreplies.config import get_settings
from autoreplies.parsers.base import ParsedLead
from autoreplies.pipeline.process_lead import process_lead
from autoreplies.pipeline.strategies import (
    PipelineStrategies,
    SendResult,
)
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import get_schema
from autoreplies.services.gmail import GmailClient
from autoreplies.services.llm import LLMClient

# ── parser_used mapping ───────────────────────────────────────────────────────

_PARSER_USED_TO_DRAFT: dict[str, Literal["regex", "llm_fallback"]] = {
    "streeteasy": "regex",
    "zillow": "regex",
    "llm_fallback": "llm_fallback",
}


def _draft_parser_used(parser_used: str) -> Literal["regex", "llm_fallback"]:
    return _PARSER_USED_TO_DRAFT.get(parser_used, "llm_fallback")


# ── DraftSend ─────────────────────────────────────────────────────────────────


class DraftSend:
    """Harness send strategy: writes a Drafts row instead of sending via Gmail."""

    def __init__(self, airtable: AirtableClient) -> None:
        self._airtable = airtable

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
        draft_id = self._airtable.create_draft(
            inquiry_record_id=inquiry_record_id,
            gmail_message_id=gmail_message_id,
            recipient=to,
            subject=subject,
            body_plaintext=plaintext_body,
            body_html=html_body,
            source=parsed.source,
            parser_used=_draft_parser_used(parsed.parser_used),
            template_source=template_source,
            reply_route=reply_route,
            apartment_match_strategy=apartment_match_strategy,
            llm_model=llm_model,
            sender=mailbox_email,
            notes_warnings=notes,
            skipped_reason=skipped_reason,
            apartment_match_confidence=apartment_match_confidence,
            llm_latency_ms=llm_latency_ms,
            would_send_at=None,  # timing not enforced in harness; H4e can wire if needed
        )
        return SendResult(sent_id=draft_id)


# ── NoopSlack ─────────────────────────────────────────────────────────────────


class NoopSlack:
    """Harness Slack strategy: does nothing."""

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
        return ""

    def post_alert(self, *, summary: str, details: dict[str, Any]) -> str:
        return ""


# ── NoopSupabase ──────────────────────────────────────────────────────────────


class NoopSupabase:
    """Harness Supabase strategy: does nothing."""

    def upsert_inquiry(self, *, id: str, **fields: Any) -> dict[str, Any]:
        return {}


# ── Factories ─────────────────────────────────────────────────────────────────


def build_harness_airtable_client() -> AirtableClient:
    """Construct an AirtableClient pointed at the test base."""
    settings = get_settings()
    schema = get_schema(settings.harness_airtable_base_id)
    return AirtableClient(token=settings.airtable_token, schema=schema)


def build_production_airtable_client_readonly() -> AirtableClient:
    """Construct an AirtableClient pointed at the production base.

    Diff path only. Never writes.
    """
    settings = get_settings()
    schema = get_schema(settings.airtable_base_id)
    return AirtableClient(token=settings.airtable_token, schema=schema)


def build_harness_strategies(airtable: AirtableClient) -> PipelineStrategies:
    """Wire the harness strategy bundle: DraftSend + NoopSlack + NoopSupabase."""
    return PipelineStrategies(
        send=DraftSend(airtable=airtable),
        slack=NoopSlack(),
        supabase=NoopSupabase(),
    )


def build_harness_pipeline() -> Callable[[str, str], None]:
    """Return a run(message_id, mailbox) callable wired with harness strategies.

    The AirtableClient and LLMClient are constructed once and shared across calls.
    GmailClient is per-mailbox (domain-wide delegation is bound at construction),
    so it is constructed fresh on each run() invocation.
    """
    settings = get_settings()
    airtable = build_harness_airtable_client()
    strategies = build_harness_strategies(airtable)
    llm = LLMClient(api_key=settings.anthropic_api_key, model=settings.anthropic_model)

    def run(message_id: str, mailbox: str) -> None:
        gmail = GmailClient(
            mailbox_email=mailbox,
            credentials_path=settings.google_application_credentials,
        )
        process_lead(
            message_id,
            mailbox,
            strategies=strategies,
            gmail=gmail,
            airtable=airtable,
            llm=llm,
        )

    return run
