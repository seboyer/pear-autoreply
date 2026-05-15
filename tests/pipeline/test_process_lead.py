"""Fixture-driven end-to-end tests for pipeline/process_lead.py.

Tests verify that process_lead, when wired with harness strategies, drives the
full parse → match → template → draft flow and materialises the right Drafts row.
Real .eml fixtures from fixtures/anonymized/ are used so the parsers run against
actual email bytes — only the downstream Airtable/LLM calls are mocked.
"""

from __future__ import annotations

import email as email_lib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from autoreplies.harness.pipeline import build_harness_strategies
from autoreplies.pipeline.process_lead import process_lead
from autoreplies.pipeline.strategies import PipelineStrategies
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import TEST

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "anonymized"


# ── Shared mock builders ──────────────────────────────────────────────────────


def _load_fixture_bytes(relative: str) -> bytes:
    return (FIXTURES_DIR / relative).read_bytes()


def _mock_gmail(fixture_relative: str, thread_id: str = "thread-abc") -> MagicMock:
    """GmailClient mock that returns the fixture email bytes on get_message."""
    raw = _load_fixture_bytes(fixture_relative)
    msg = email_lib.message_from_bytes(raw)
    gmail = MagicMock()
    gmail.get_message.return_value = (msg, thread_id)
    return gmail


def _mock_airtable(
    *,
    agent_record: dict[str, Any] | None = None,
    apartment_record: dict[str, Any] | None = None,
    inquiry_id: str = "recINQ_TEST",
) -> AirtableClient:
    """AirtableClient mock wired with TEST schema."""
    at = MagicMock(spec=AirtableClient)
    at.schema = TEST

    # Agent lookup
    if agent_record is None:
        agent_record = {
            "id": "recAGENT1",
            "fields": {
                TEST.users.name: "Garland Agent",
                TEST.users.email: "garland@pearnyc.com",
                TEST.users.autoreply_test_template: "",  # use fallback template
            },
        }
    at.find_monitored_user_by_primary_email.return_value = agent_record

    # Apartment matching
    at.match_apartment_by_streeteasy_id.return_value = apartment_record
    at.match_apartment_by_address.return_value = apartment_record

    # User matching
    at.find_existing_user.return_value = None

    # Inquiry creation
    at.find_inquiry_by_gmail_message_id.return_value = None
    at.find_or_create_inquiry.return_value = inquiry_id

    # Draft creation
    at.create_draft.return_value = "recDRAFT_TEST"

    return at


def _mock_llm(filled_body: str = "Hi there, thanks for reaching out!") -> MagicMock:
    llm = MagicMock()
    llm.model = "claude-haiku-4-5-20251001"
    llm.fill_template.return_value = {
        "filled_body": filled_body,
        "model": "claude-haiku-4-5-20251001",
        "latency_ms": "350",
        "strategy": "llm",
    }
    return llm


def _harness_strategies(airtable: AirtableClient) -> PipelineStrategies:
    return build_harness_strategies(airtable)


# ── StreetEasy fixture — tour variant ─────────────────────────────────────────


def test_streeteasy_tour_fixture_creates_draft_row() -> None:
    """Feed a real StreetEasy tour .eml — assert Drafts row written with expected fields."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    gmail = _mock_gmail(fixture, thread_id="thread-se-001")
    airtable = _mock_airtable(inquiry_id="recINQ_SE")
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)

    process_lead(
        "gmail-msg-se-001",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=gmail,
        airtable=airtable,
        llm=llm,
    )

    airtable.find_or_create_inquiry.assert_called_once()
    airtable.create_draft.assert_called_once()
    kwargs = airtable.create_draft.call_args.kwargs

    assert kwargs["gmail_message_id"] == "gmail-msg-se-001"
    assert kwargs["inquiry_record_id"] == "recINQ_SE"
    assert kwargs["source"] == "StreetEasy"
    assert kwargs["parser_used"] == "regex"
    assert kwargs["reply_route"] == "thread"
    assert kwargs["recipient"] == "xugrace10@gmail.com"
    assert kwargs["subject"] == "Re: 65 Saint Mark's Avenue #2B StreetEasy Inquiry From Grace Xu"
    assert kwargs["skipped_reason"] is None
    assert kwargs["llm_model"] == "claude-haiku-4-5-20251001"


def test_streeteasy_tour_calls_gmail_get_message() -> None:
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    gmail = _mock_gmail(fixture)
    airtable = _mock_airtable()
    llm = _mock_llm()

    process_lead(
        "gmail-msg-x",
        "garland@pearnyc.com",
        strategies=_harness_strategies(airtable),
        gmail=gmail,
        airtable=airtable,
        llm=llm,
    )

    gmail.get_message.assert_called_once_with("gmail-msg-x")


def test_streeteasy_tour_calls_llm_fill_template() -> None:
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    gmail = _mock_gmail(fixture)
    airtable = _mock_airtable()
    llm = _mock_llm()

    process_lead(
        "gmail-msg-x",
        "garland@pearnyc.com",
        strategies=_harness_strategies(airtable),
        gmail=gmail,
        airtable=airtable,
        llm=llm,
    )

    llm.fill_template.assert_called_once()
    slots = llm.fill_template.call_args.kwargs["slots"]
    # Grace Xu is extracted from the StreetEasy subject
    assert slots["first_name"] == "Grace"
    assert "Saint Mark" in (slots["apartment_address"] or "")


# ── Zillow fixture ────────────────────────────────────────────────────────────


def test_zillow_fixture_creates_draft_row() -> None:
    """Feed a real Zillow .eml — assert Drafts row has Zillow-specific fields."""
    fixture = "zillow/lead__170-prospect-pl-3b__1.eml"
    gmail = _mock_gmail(fixture, thread_id="thread-zl-001")
    airtable = _mock_airtable(inquiry_id="recINQ_ZL")
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)

    process_lead(
        "gmail-msg-zl-001",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=gmail,
        airtable=airtable,
        llm=llm,
    )

    airtable.create_draft.assert_called_once()
    kwargs = airtable.create_draft.call_args.kwargs

    assert kwargs["source"] == "Zillow"
    assert kwargs["parser_used"] == "regex"
    assert kwargs["reply_route"] == "thread"
    assert kwargs["recipient"] == "ecbrown2@gmail.com"
    assert "170 Prospect" in kwargs["subject"]


def test_zillow_fixture_listing_id_is_none() -> None:
    """Zillow leads always have listing_id=None — apartment match uses address."""
    fixture = "zillow/lead__170-prospect-pl-3b__1.eml"
    gmail = _mock_gmail(fixture)
    airtable = _mock_airtable()
    llm = _mock_llm()

    process_lead(
        "gmail-msg-zl-002",
        "garland@pearnyc.com",
        strategies=_harness_strategies(airtable),
        gmail=gmail,
        airtable=airtable,
        llm=llm,
    )

    # listing_id=None so streeteasy_id match is never attempted.
    airtable.match_apartment_by_streeteasy_id.assert_not_called()


# ── Skipped reply route ───────────────────────────────────────────────────────


def test_skipped_route_when_no_email() -> None:
    """When no Reply-To and no email on parsed lead, route=skipped is recorded."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    raw = _load_fixture_bytes(fixture)

    # Reconstruct with no Reply-To header.
    msg = email_lib.message_from_bytes(raw)
    del msg["Reply-To"]
    # Patch the parser to return a lead with email=None.
    from autoreplies.parsers.base import ParsedLead
    mock_parsed = ParsedLead(
        source="StreetEasy",
        first_name="Grace",
        last_name="Xu",
        email=None,
        phone=None,
        apartment_address="65 Saint Mark's Avenue #2B",
        listing_id=None,
        listing_url=None,
        message_body=None,
        parser_used="streeteasy",
    )

    gmail = MagicMock()
    gmail.get_message.return_value = (msg, "thread-001")

    airtable = _mock_airtable(inquiry_id="recINQ_SKIP")
    llm = _mock_llm()

    with patch("autoreplies.pipeline.process_lead.parsers_base.parse", return_value=mock_parsed):
        process_lead(
            "gmail-msg-skip",
            "garland@pearnyc.com",
            strategies=_harness_strategies(airtable),
            gmail=gmail,
            airtable=airtable,
            llm=llm,
        )

    kwargs = airtable.create_draft.call_args.kwargs
    assert kwargs["reply_route"] == "skipped"
    assert kwargs["skipped_reason"] is not None


# ── Production strategies still raise ────────────────────────────────────────


def test_production_livesend_still_raises() -> None:
    """The production strategy path must still raise NotImplementedError (Phase 2)."""
    from autoreplies.pipeline.strategies import build_production_strategies

    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    gmail = _mock_gmail(fixture)
    airtable = _mock_airtable()
    llm = _mock_llm()

    with pytest.raises(NotImplementedError):
        process_lead(
            "gmail-msg-prod",
            "garland@pearnyc.com",
            strategies=build_production_strategies(),
            gmail=gmail,
            airtable=airtable,
            llm=llm,
        )


# ── No services → raises NotImplementedError ─────────────────────────────────


def test_no_services_raises_not_implemented() -> None:
    """When no services are passed (production path), phase A raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        process_lead("msg-id", "agent@pearnyc.com")
