"""End-to-end integration tests for the harness pipeline.

Each test loads a real .eml fixture, mocks all I/O, and drives process_lead
through build_harness_strategies. Asserts that the full chain
(parse → match → template → DraftSend → create_draft) runs without errors
and writes the expected Drafts row fields.
"""

from __future__ import annotations

import email
from email import policy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from autoreplies.harness.pipeline import NoopSlack, NoopSupabase, build_harness_strategies
from autoreplies.pipeline.process_lead import process_lead
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import TEST
from autoreplies.services.gmail import GmailClient
from autoreplies.services.llm import LLMClient, TemplateFillError

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "anonymized"

FILLED_BODY = "Hi there, thanks for your interest in the listing."
FAKE_TEMPLATE = "Hi {{first_name|there}}, thanks for your interest in {{apartment_address|the listing}}."
FAKE_MAILBOX = "agent@pearnyc.com"
FAKE_MESSAGE_ID = "msg-integration-001"


def _load_fixture(relative_path: str) -> Any:
    raw = (FIXTURES_DIR / relative_path).read_bytes()
    return email.message_from_bytes(raw, policy=policy.default)


def _make_airtable(*, apartment_via_streeteasy: bool = True) -> AirtableClient:
    """Build a fully-mocked AirtableClient against the TEST schema."""
    mock = MagicMock(spec=AirtableClient)
    mock.schema = TEST

    fake_apt = {"id": "recAPT_FAKE", "fields": {TEST.apartments.full_address: "267 Clifton Place #1A"}}
    if apartment_via_streeteasy:
        mock.match_apartment_by_streeteasy_id.return_value = fake_apt
        mock.match_apartment_by_address.return_value = None
    else:
        mock.match_apartment_by_streeteasy_id.return_value = None
        mock.match_apartment_by_address.return_value = fake_apt

    mock.find_existing_user.return_value = None
    mock.find_monitored_user_by_primary_email.return_value = {
        "id": "recAGENT_FAKE",
        "fields": {TEST.users.autoreply_test_template: FAKE_TEMPLATE},
    }
    mock.find_or_create_inquiry.return_value = "recINQ_FAKE"
    mock.create_draft.return_value = "recDRAFT_FAKE"
    return mock


def _make_llm(*, raise_error: bool = False) -> LLMClient:
    mock = MagicMock(spec=LLMClient)
    mock.model = "claude-haiku-4-5-20251001"
    if raise_error:
        mock.fill_template.side_effect = TemplateFillError("missing slot: first_name")
    else:
        mock.fill_template.return_value = {
            "filled_body": FILLED_BODY,
            "model": "claude-haiku-4-5-20251001",
            "latency_ms": "250",
        }
    return mock


def _make_gmail(fixture_path: str) -> GmailClient:
    msg = _load_fixture(fixture_path)
    mock = MagicMock(spec=GmailClient)
    mock.get_message.return_value = (msg, "thread-fake-123")
    return mock


# ── golden path: StreetEasy tour ─────────────────────────────────────────────

def test_streeteasy_golden_path() -> None:
    """Full pipeline run on a real StreetEasy tour fixture — asserts Drafts row written."""
    mock_airtable = _make_airtable(apartment_via_streeteasy=True)
    mock_llm = _make_llm()
    mock_gmail = _make_gmail("streeteasy/tour__267-clifton-place-1a__0.eml")
    strategies = build_harness_strategies(mock_airtable)

    process_lead(
        FAKE_MESSAGE_ID,
        FAKE_MAILBOX,
        strategies=strategies,
        gmail=mock_gmail,
        airtable=mock_airtable,
        llm=mock_llm,
    )

    mock_airtable.find_or_create_inquiry.assert_called_once()
    mock_airtable.create_draft.assert_called_once()

    call_kw = mock_airtable.create_draft.call_args.kwargs
    assert call_kw["reply_route"] == "thread"
    assert call_kw["source"] == "StreetEasy"
    assert call_kw["parser_used"] == "regex"
    assert call_kw["body_plaintext"] == FILLED_BODY
    assert call_kw["apartment_match_strategy"] == "streeteasy_id"
    assert call_kw["gmail_message_id"] == FAKE_MESSAGE_ID
    assert call_kw["inquiry_record_id"] == "recINQ_FAKE"

    # Production side-effects must be no-ops
    assert isinstance(strategies.slack, NoopSlack)
    assert isinstance(strategies.supabase, NoopSupabase)


# ── golden path: Zillow ───────────────────────────────────────────────────────

def test_zillow_golden_path() -> None:
    """Full pipeline run on a real Zillow fixture — asserts Drafts row written."""
    mock_airtable = _make_airtable(apartment_via_streeteasy=False)
    mock_llm = _make_llm()
    mock_gmail = _make_gmail("zillow/lead__112-30-rockaway-beach-blvd-2a__38642.eml")
    strategies = build_harness_strategies(mock_airtable)

    process_lead(
        FAKE_MESSAGE_ID,
        FAKE_MAILBOX,
        strategies=strategies,
        gmail=mock_gmail,
        airtable=mock_airtable,
        llm=mock_llm,
    )

    mock_airtable.find_or_create_inquiry.assert_called_once()
    mock_airtable.create_draft.assert_called_once()

    call_kw = mock_airtable.create_draft.call_args.kwargs
    assert call_kw["reply_route"] == "thread"
    assert call_kw["source"] == "Zillow"
    assert call_kw["parser_used"] == "regex"
    assert call_kw["body_plaintext"] == FILLED_BODY
    assert call_kw["apartment_match_strategy"] == "address"
    assert call_kw["gmail_message_id"] == FAKE_MESSAGE_ID

    assert isinstance(strategies.slack, NoopSlack)
    assert isinstance(strategies.supabase, NoopSupabase)


# ── skipped route: TemplateFillError ─────────────────────────────────────────

def test_streeteasy_skipped_on_template_fill_error() -> None:
    """When LLM fill raises TemplateFillError, route is skipped and Drafts row still written."""
    mock_airtable = _make_airtable(apartment_via_streeteasy=True)
    mock_llm = _make_llm(raise_error=True)
    mock_gmail = _make_gmail("streeteasy/tour__267-clifton-place-1a__0.eml")
    strategies = build_harness_strategies(mock_airtable)

    process_lead(
        FAKE_MESSAGE_ID,
        FAKE_MAILBOX,
        strategies=strategies,
        gmail=mock_gmail,
        airtable=mock_airtable,
        llm=mock_llm,
    )

    mock_airtable.create_draft.assert_called_once()

    call_kw = mock_airtable.create_draft.call_args.kwargs
    assert call_kw["reply_route"] == "skipped"
    assert call_kw["skipped_reason"] is not None
    assert call_kw["skipped_reason"].startswith("template_fill_error:")
    assert call_kw["body_plaintext"] == ""
    assert call_kw["recipient"] == ""
    # Inquiry is still created even for skipped leads
    mock_airtable.find_or_create_inquiry.assert_called_once()
