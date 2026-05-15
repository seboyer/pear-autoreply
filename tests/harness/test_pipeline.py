"""Tests for harness/pipeline.py — DraftSend, NoopSlack, NoopSupabase strategies."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from autoreplies.harness.pipeline import (
    DraftSend,
    NoopSlack,
    NoopSupabase,
    _draft_parser_used,
    build_harness_strategies,
)
from autoreplies.parsers.base import ParsedLead
from autoreplies.pipeline.strategies import PipelineStrategies, SendResult
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import (
    TEST,
)

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_airtable() -> AirtableClient:
    client = MagicMock(spec=AirtableClient)
    client.schema = TEST
    client.create_draft.return_value = "recDRAFT_H4"
    return client


@pytest.fixture()
def draft_send(mock_airtable: AirtableClient) -> DraftSend:
    return DraftSend(airtable=mock_airtable)


def _make_parsed(source: str = "StreetEasy", parser_used: str = "streeteasy") -> ParsedLead:
    return ParsedLead(
        source=source,  # type: ignore[arg-type]
        first_name="Jane",
        last_name="Smith",
        email="jane@example.com",
        phone="212-555-0000",
        apartment_address="123 Main St",
        listing_url="https://streeteasy.com/rental/1234567",
        listing_id="1234567",
        message_body="Is this available?",
        parser_used=parser_used,
    )


def _send_kwargs(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = dict(
        to="jane@example.com",
        subject="Re: 123 Main St",
        plaintext_body="Hi Jane…",
        html_body="<p>Hi Jane…</p>",
        in_reply_to_message_id="orig-msg-id",
        thread_id="thread-xyz",
        agent={"id": "recAGENT1", "fields": {}},
        parsed=_make_parsed(),
        inquiry_record_id="recINQ_42",
        gmail_message_id="msg-original-123",
        reply_route="thread",
        skipped_reason=None,
        apartment_match_strategy="streeteasy_id",
        apartment_match_confidence=100,
        template_source="agent",
        llm_model="claude-haiku-4-5-20251001",
        llm_latency_ms=450,
        notes="",
    )
    defaults.update(overrides)
    return defaults


# ── parser_used mapping ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "parser_used,expected",
    [
        ("streeteasy", "regex"),
        ("zillow", "regex"),
        ("llm_fallback", "llm_fallback"),
        ("unknown", "llm_fallback"),  # safe default
    ],
)
def test_draft_parser_used_mapping(parser_used: str, expected: str) -> None:
    assert _draft_parser_used(parser_used) == expected


# ── DraftSend ─────────────────────────────────────────────────────────────────


def test_draft_send_returns_send_result(
    draft_send: DraftSend, mock_airtable: AirtableClient
) -> None:
    result = draft_send.send_reply(**_send_kwargs())
    assert isinstance(result, SendResult)
    assert result.sent_id == "recDRAFT_H4"


def test_draft_send_calls_create_draft(
    draft_send: DraftSend, mock_airtable: AirtableClient
) -> None:
    draft_send.send_reply(**_send_kwargs())
    mock_airtable.create_draft.assert_called_once()
    call_kwargs = mock_airtable.create_draft.call_args.kwargs
    assert call_kwargs["inquiry_record_id"] == "recINQ_42"
    assert call_kwargs["gmail_message_id"] == "msg-original-123"
    assert call_kwargs["recipient"] == "jane@example.com"
    assert call_kwargs["subject"] == "Re: 123 Main St"
    assert call_kwargs["body_plaintext"] == "Hi Jane…"
    assert call_kwargs["body_html"] == "<p>Hi Jane…</p>"
    assert call_kwargs["source"] == "StreetEasy"
    assert call_kwargs["parser_used"] == "regex"  # streeteasy → regex mapping
    assert call_kwargs["template_source"] == "agent"
    assert call_kwargs["reply_route"] == "thread"
    assert call_kwargs["apartment_match_strategy"] == "streeteasy_id"
    assert call_kwargs["apartment_match_confidence"] == 100
    assert call_kwargs["llm_model"] == "claude-haiku-4-5-20251001"
    assert call_kwargs["llm_latency_ms"] == 450
    assert call_kwargs["skipped_reason"] is None


def test_draft_send_zillow_parser_maps_to_regex(
    draft_send: DraftSend, mock_airtable: AirtableClient
) -> None:
    zillow_parsed = _make_parsed(source="Zillow", parser_used="zillow")
    draft_send.send_reply(**_send_kwargs(parsed=zillow_parsed))
    call_kwargs = mock_airtable.create_draft.call_args.kwargs
    assert call_kwargs["source"] == "Zillow"
    assert call_kwargs["parser_used"] == "regex"


def test_draft_send_skipped_route(draft_send: DraftSend, mock_airtable: AirtableClient) -> None:
    draft_send.send_reply(
        **_send_kwargs(
            reply_route="skipped",
            skipped_reason="no Reply-To and parsed.email is None",
            to="",
        )
    )
    call_kwargs = mock_airtable.create_draft.call_args.kwargs
    assert call_kwargs["reply_route"] == "skipped"
    assert call_kwargs["skipped_reason"] == "no Reply-To and parsed.email is None"


# ── NoopSlack ─────────────────────────────────────────────────────────────────


def test_noop_slack_post_lead_returns_empty_string() -> None:
    slack = NoopSlack()
    result = slack.post_lead(
        source="StreetEasy",
        agent_name="Sam",
        agent_email="sam@pearnyc.com",
        prospect_name="Jane",
        prospect_email="jane@example.com",
        prospect_phone=None,
        apartment_address="123 Main St",
        apartment_match_confidence=97,
        message_excerpt="Is this available?",
        airtable_record_id="recINQ1",
        gmail_thread_url="https://mail.google.com/...",
    )
    assert result == ""


def test_noop_slack_post_alert_returns_empty_string() -> None:
    assert NoopSlack().post_alert(summary="test", details={}) == ""


# ── NoopSupabase ──────────────────────────────────────────────────────────────


def test_noop_supabase_returns_empty_dict() -> None:
    result = NoopSupabase().upsert_inquiry(id="recINQ1", gmail_message_id="msg-x")
    assert result == {}


# ── build_harness_strategies ──────────────────────────────────────────────────


def test_build_harness_strategies_returns_pipeline_strategies(
    mock_airtable: AirtableClient,
) -> None:
    strategies = build_harness_strategies(mock_airtable)
    assert isinstance(strategies, PipelineStrategies)
    assert isinstance(strategies.send, DraftSend)
    assert isinstance(strategies.slack, NoopSlack)
    assert isinstance(strategies.supabase, NoopSupabase)
