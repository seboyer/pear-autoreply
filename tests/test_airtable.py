"""Tests for AirtableClient — Phase 3 implementation.

All HTTP is mocked via unittest.mock. Tests do not hit Airtable.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from autoreplies.parsers.base import ParsedLead
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import PROD, TEST


@pytest.fixture()
def client() -> AirtableClient:
    return AirtableClient(token="fake-token", schema=PROD)


@pytest.fixture()
def test_client() -> AirtableClient:
    return AirtableClient(token="fake-token", schema=TEST)


def _mock_table(rows: list[dict[str, Any]]) -> MagicMock:
    tbl = MagicMock()
    tbl.all.return_value = rows
    return tbl


def _record(rec_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"id": rec_id, "fields": fields}


# ── find_monitored_user_by_autoreply_email ────────────────────────────────────


def test_find_monitored_user_found(client: AirtableClient) -> None:
    row = _record("recAGENT1", {PROD.users.autoreply_email_agent: "sam@pearnyc.com"})
    with patch.object(client, "_table", return_value=_mock_table([row])):
        result = client.find_monitored_user_by_autoreply_email("sam@pearnyc.com")
    assert result == row


def test_find_monitored_user_not_found(client: AirtableClient) -> None:
    with patch.object(client, "_table", return_value=_mock_table([])):
        assert client.find_monitored_user_by_autoreply_email("nobody@pearnyc.com") is None


def test_find_monitored_user_uses_autoreply_email_field(client: AirtableClient) -> None:
    """Formula must filter on autoreply_email_agent, not the primary email field."""
    tbl = MagicMock()
    tbl.all.return_value = []
    with patch.object(client, "_table", return_value=tbl):
        client.find_monitored_user_by_autoreply_email("sam@pearnyc.com")
    formula_str = str(tbl.all.call_args.kwargs["formula"])
    # Must reference autoreply_email_agent field ID
    assert PROD.users.autoreply_email_agent in formula_str
    # Must NOT reference the primary email field
    assert PROD.users.email not in formula_str


# ── list_monitored_primary_emails ─────────────────────────────────────────────


def test_list_monitored_primary_emails_returns_sorted_distinct(client: AirtableClient) -> None:
    rows = [
        _record("recAGENT1", {PROD.users.email: "b@pearnyc.com"}),
        _record("recAGENT2", {PROD.users.email: "a@pearnyc.com"}),
        _record("recAGENT3", {PROD.users.email: "a@pearnyc.com"}),  # dup
    ]
    with patch.object(client, "_table", return_value=_mock_table(rows)):
        assert client.list_monitored_primary_emails() == ["a@pearnyc.com", "b@pearnyc.com"]


def test_list_monitored_primary_emails_filters_empty(client: AirtableClient) -> None:
    rows = [
        _record("recAGENT1", {PROD.users.email: "a@pearnyc.com"}),
        _record("recAGENT2", {}),
        _record("recAGENT3", {PROD.users.email: ""}),
    ]
    with patch.object(client, "_table", return_value=_mock_table(rows)):
        assert client.list_monitored_primary_emails() == ["a@pearnyc.com"]


def test_list_monitored_primary_emails_empty(client: AirtableClient) -> None:
    with patch.object(client, "_table", return_value=_mock_table([])):
        assert client.list_monitored_primary_emails() == []


# ── list_monitored_autoreply_inboxes ──────────────────────────────────────────


def test_list_monitored_autoreply_inboxes_returns_sorted_distinct(client: AirtableClient) -> None:
    rows = [
        _record("recAGENT1", {PROD.users.autoreply_email_agent: "b@pearnyc.com"}),
        _record("recAGENT2", {PROD.users.autoreply_email_agent: "a@pearnyc.com"}),
        _record("recAGENT3", {PROD.users.autoreply_email_agent: "a@pearnyc.com"}),  # dup
    ]
    with patch.object(client, "_table", return_value=_mock_table(rows)):
        assert client.list_monitored_autoreply_inboxes() == ["a@pearnyc.com", "b@pearnyc.com"]


def test_list_monitored_autoreply_inboxes_warns_on_blank(
    client: AirtableClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A row with Autoreply Enabled but no Autoreply Email (Agent) is skipped with WARNING."""
    rows = [
        _record("recAGENT1", {PROD.users.autoreply_email_agent: "a@pearnyc.com"}),
        _record("recAGENT2", {}),  # blank inbox — misconfigured
    ]
    with (
        patch.object(client, "_table", return_value=_mock_table(rows)),
        caplog.at_level(logging.WARNING, logger="autoreplies.services.airtable"),
    ):
        result = client.list_monitored_autoreply_inboxes()
    assert result == ["a@pearnyc.com"]
    assert any("recAGENT2" in record.message for record in caplog.records)


def test_list_monitored_autoreply_inboxes_empty(client: AirtableClient) -> None:
    with patch.object(client, "_table", return_value=_mock_table([])):
        assert client.list_monitored_autoreply_inboxes() == []


# ── find_existing_user ────────────────────────────────────────────────────────


def test_find_user_by_email(client: AirtableClient) -> None:
    row = _record("recUSER1", {PROD.users.email: "prospect@example.com"})
    with patch.object(client, "_table", return_value=_mock_table([row])):
        result = client.find_existing_user(email="prospect@example.com")
    assert result == row


def test_find_user_by_phone(client: AirtableClient) -> None:
    row = _record("recUSER2", {PROD.users.phone: "555-1234"})
    with patch.object(client, "_table", return_value=_mock_table([row])):
        result = client.find_existing_user(phone="555-1234")
    assert result == row


def test_find_user_no_args_returns_none(client: AirtableClient) -> None:
    assert client.find_existing_user() is None


def test_find_user_not_found(client: AirtableClient) -> None:
    with patch.object(client, "_table", return_value=_mock_table([])):
        assert client.find_existing_user(email="ghost@example.com") is None


def test_find_existing_user_excludes_admins(client: AirtableClient) -> None:
    """Admin rows must never be returned as prospect matches."""
    tbl = MagicMock()
    tbl.all.return_value = []
    with patch.object(client, "_table", return_value=tbl):
        result = client.find_existing_user(email="admin@pearnyc.com")
    assert result is None
    formula_str = str(tbl.all.call_args.kwargs["formula"])
    assert "Admin" in formula_str


# ── match_apartment_by_streeteasy_id ──────────────────────────────────────────


def test_match_apartment_streeteasy_found(client: AirtableClient) -> None:
    row = _record("recAPT1", {PROD.apartments.streeteasy: "https://streeteasy.com/rental/1234567"})
    with patch.object(client, "_table", return_value=_mock_table([row])):
        result = client.match_apartment_by_streeteasy_id("1234567")
    assert result == row


def test_match_apartment_streeteasy_not_found(client: AirtableClient) -> None:
    with patch.object(client, "_table", return_value=_mock_table([])):
        assert client.match_apartment_by_streeteasy_id("9999999") is None


# ── match_apartment_by_address ────────────────────────────────────────────────


def _apt_row(rec_id: str, full_address: str) -> dict[str, Any]:
    return _record(rec_id, {PROD.apartments.full_address: full_address})


@pytest.mark.parametrize(
    "parsed_address,stored_address,should_match",
    [
        # Canonical: abbreviation expansion both sides → exact match
        ("353 Flatbush Avenue #4R", "353 Flatbush Ave 4R, Brooklyn, NY, 11238", True),
        # Mac→Mc canonicalization
        ("96 Macdonough St #2", "96 Mcdonough St 2, Brooklyn, NY, 11233", True),
        # Queens hyphen collapse on stored side
        ("2106 Linden St #3E", "21-06 Linden St 3E, Brooklyn, NY, 11385", True),
        # Fuzzy street (typo "Bergan" vs "Bergen") — should score ≥ 88
        ("1965 Bergan Street #1B", "1965 Bergen St 1B, Brooklyn, NY, 11233", True),
        # Unit mismatch — exact unit comparison fails
        ("162 Covert St #2L", "162 Covert St 2A, Brooklyn, NY, 11221", False),
        ("162 Covert St #2L", "162 Covert St 2B, Brooklyn, NY, 11221", False),
        # House number mismatch
        ("354 Flatbush Avenue #4R", "353 Flatbush Ave 4R, Brooklyn, NY, 11238", False),
        # Completely different address
        ("123 Main St #1A", "999 Unrelated Ave 5B, Brooklyn, NY, 11201", False),
    ],
)
def test_match_apartment_address_parametrized(
    client: AirtableClient,
    parsed_address: str,
    stored_address: str,
    should_match: bool,
) -> None:
    row = _apt_row("recAPTx", stored_address)
    with patch.object(client, "_table", return_value=_mock_table([row])):
        result = client.match_apartment_by_address(parsed_address)
    if should_match:
        assert result is not None
        record, score = result
        assert record == row
        assert isinstance(score, int)
        assert score >= 88
    else:
        assert result is None


def test_match_apartment_address_empty_table(client: AirtableClient) -> None:
    with patch.object(client, "_table", return_value=_mock_table([])):
        assert client.match_apartment_by_address("123 Main St #1A") is None


def test_match_apartment_address_returns_best_of_multiple_candidates(
    client: AirtableClient,
) -> None:
    """When multiple rows share the same house + unit, the highest-scoring street wins."""
    rows = [
        _apt_row("recAPT_CLOSE", "353 Flatbush Ave 4R, Brooklyn, NY, 11238"),
        _apt_row("recAPT_FAR", "353 Flatbush Court 4R, Brooklyn, NY, 11238"),
    ]
    with patch.object(client, "_table", return_value=_mock_table(rows)):
        result = client.match_apartment_by_address("353 Flatbush Avenue #4R")
    assert result is not None
    record, _score = result
    assert record["id"] == "recAPT_CLOSE"


def test_match_apartment_address_unparseable_returns_none(client: AirtableClient) -> None:
    """Addresses that don't split (no house number or no unit) return None immediately."""
    with patch.object(client, "_table", return_value=_mock_table([])) as mock_tbl:
        result = client.match_apartment_by_address("just some words without structure")
    assert result is None
    # Airtable should not be queried when the address can't be parsed
    mock_tbl.all.assert_not_called()


def test_match_apartment_address_caches_splits(client: AirtableClient) -> None:
    """Second call reuses cached splits instead of re-normalizing stored rows."""
    row = _apt_row("recAPT_CACHE", "100 Main St 2A, Brooklyn, NY, 11201")
    with patch.object(client, "_table", return_value=_mock_table([row])):
        client.match_apartment_by_address("100 Main Street #2A")
        client.match_apartment_by_address("100 Main Street #2A")
    # Cache should have been populated after first call
    assert "recAPT_CACHE" in client._apt_split_cache


# ── find_inquiry_by_gmail_message_id ──────────────────────────────────────────


def test_find_inquiry_found(client: AirtableClient) -> None:
    row = _record("recINQ1", {PROD.inquiries.gmail_message_id_autoreply: "msg-abc-123"})
    with patch.object(client, "_table", return_value=_mock_table([row])):
        assert client.find_inquiry_by_gmail_message_id("msg-abc-123") == row


def test_find_inquiry_not_found(client: AirtableClient) -> None:
    with patch.object(client, "_table", return_value=_mock_table([])):
        assert client.find_inquiry_by_gmail_message_id("msg-missing") is None


# ── create_inquiry ─────────────────────────────────────────────────────────────


@pytest.fixture()
def streeteasy_lead() -> ParsedLead:
    return ParsedLead(
        source="StreetEasy",
        first_name="Jane",
        last_name="Smith",
        email="jane@example.com",
        phone="212-555-9999",
        apartment_address="123 Main St",
        listing_url="https://streeteasy.com/rental/1234567",
        listing_id="1234567",
        message_body="Is this available?",
        parser_used="streeteasy",
    )


@pytest.fixture()
def zillow_lead() -> ParsedLead:
    return ParsedLead(
        source="Zillow",
        first_name=None,
        last_name=None,
        email="zillow_user@example.com",
        phone=None,
        apartment_address="456 Broadway",
        listing_url="https://zillow.com/rental/abc",
        listing_id=None,
        message_body="Interested in this unit.",
        parser_used="zillow",
    )


def test_create_inquiry_streeteasy_full(
    client: AirtableClient, streeteasy_lead: ParsedLead
) -> None:
    inq = PROD.inquiries
    tbl = MagicMock()
    tbl.create.return_value = {"id": "recINQ_NEW"}
    with patch.object(client, "_table", return_value=tbl):
        rec_id = client.create_inquiry(
            gmail_message_id="msg-xyz",
            parsed=streeteasy_lead,
            apartment_record_id="recAPT99",
            user_record_id="recUSER99",
        )
    assert rec_id == "recINQ_NEW"
    fields = tbl.create.call_args[0][0]
    assert fields[inq.method] == "Web"
    assert fields[inq.type_non_website] == "StreetEasy"
    assert fields[inq.name_form] == "Jane Smith"
    assert fields[inq.email_form] == "jane@example.com"
    assert fields[inq.phone] == "212-555-9999"
    assert fields[inq.apartment] == ["recAPT99"]
    assert fields[inq.apartment_failsafe] == "123 Main St"
    assert fields[inq.user] == ["recUSER99"]
    assert fields[inq.gmail_message_id_autoreply] == "msg-xyz"
    # Agent must NOT be set — it is a lookup through Apartment
    assert not any("agent" in str(k).lower() for k in fields)


def test_create_inquiry_zillow_no_name_no_phone(
    client: AirtableClient, zillow_lead: ParsedLead
) -> None:
    inq = PROD.inquiries
    tbl = MagicMock()
    tbl.create.return_value = {"id": "recINQ_ZILLOW"}
    with patch.object(client, "_table", return_value=tbl):
        rec_id = client.create_inquiry(
            gmail_message_id="msg-zillow",
            parsed=zillow_lead,
            apartment_record_id=None,
            user_record_id=None,
        )
    assert rec_id == "recINQ_ZILLOW"
    fields = tbl.create.call_args[0][0]
    assert fields[inq.type_non_website] == "Zillow"
    assert fields[inq.name_form] == ""
    assert inq.phone not in fields
    assert inq.apartment not in fields
    assert fields[inq.apartment_failsafe] == "456 Broadway"
    assert inq.user not in fields


# ── Formula escaping (regression — was injection-prone via f-strings) ─────────


def test_find_existing_user_escapes_single_quotes(client: AirtableClient) -> None:
    """A single quote in user input must be escaped, not propagated raw."""
    tbl = MagicMock()
    tbl.all.return_value = []
    with patch.object(client, "_table", return_value=tbl):
        client.find_existing_user(email="o'malley@example.com")
    formula_str = str(tbl.all.call_args.kwargs["formula"])
    # Pyairtable escapes ' as \' inside quoted string literals.
    assert r"o\'malley@example.com" in formula_str
    # The raw, unescaped form must not appear inside a value literal.
    assert "'o'malley" not in formula_str


def test_match_apartment_streeteasy_escapes_single_quotes(client: AirtableClient) -> None:
    """A crafted listing_id containing a quote must not break the FIND() formula."""
    tbl = MagicMock()
    tbl.all.return_value = []
    with patch.object(client, "_table", return_value=tbl):
        client.match_apartment_by_streeteasy_id("12345', '")
    formula_str = str(tbl.all.call_args.kwargs["formula"])
    assert r"\'" in formula_str


# ── create_draft ──────────────────────────────────────────────────────────────

_SEND_AT = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)


def _draft_kwargs(**overrides: Any) -> dict[str, Any]:
    """Base kwargs for create_draft; override per test."""
    defaults: dict[str, Any] = dict(
        inquiry_record_id="recINQ_X",
        gmail_message_id="msg-draft-1",
        recipient="prospect@example.com",
        subject="Re: 123 Main St",
        body_plaintext="Hi Jane, thanks for your interest…",
        body_html="<p>Hi Jane…</p>",
        source="StreetEasy",
        parser_used="regex",
        template_source="agent",
        reply_route="thread",
        apartment_match_strategy="streeteasy_id",
        llm_model="claude-haiku-4-5-20251001",
        sender="agent@pearnyc.com",
        notes_warnings="",
    )
    defaults.update(overrides)
    return defaults


def test_create_draft_full_streeteasy(test_client: AirtableClient) -> None:
    d = TEST.drafts
    tbl = MagicMock()
    tbl.create.return_value = {"id": "recDRAFT_1"}
    with patch.object(test_client, "_table", return_value=tbl):
        rec_id = test_client.create_draft(
            **_draft_kwargs(
                apartment_match_confidence=97,
                llm_latency_ms=823,
                would_send_at=_SEND_AT,
            )
        )
    assert rec_id == "recDRAFT_1"
    fields = tbl.create.call_args[0][0]
    assert fields[d.inquiry] == ["recINQ_X"]
    assert fields[d.gmail_message_id] == "msg-draft-1"
    assert fields[d.recipient] == "prospect@example.com"
    assert fields[d.subject] == "Re: 123 Main St"
    assert fields[d.source] == "StreetEasy"
    assert fields[d.parser_used] == "regex"
    assert fields[d.template_source] == "agent"
    assert fields[d.reply_route] == "thread"
    assert fields[d.apartment_match_strategy] == "streeteasy_id"
    assert fields[d.apartment_match_confidence] == 97
    assert fields[d.llm_model] == "claude-haiku-4-5-20251001"
    assert fields[d.llm_latency_ms] == 823
    assert fields[d.would_send_at] == _SEND_AT.isoformat()
    assert fields[d.sender] == "agent@pearnyc.com"
    # skipped_reason absent when not provided
    assert d.skipped_reason not in fields


def test_create_draft_zillow_minimal(test_client: AirtableClient) -> None:
    """Optional fields absent from payload when not provided."""
    d = TEST.drafts
    tbl = MagicMock()
    tbl.create.return_value = {"id": "recDRAFT_2"}
    with patch.object(test_client, "_table", return_value=tbl):
        test_client.create_draft(
            **_draft_kwargs(
                source="Zillow",
                parser_used="regex",
                apartment_match_strategy="address",
            )
        )
    fields = tbl.create.call_args[0][0]
    assert fields[d.source] == "Zillow"
    assert d.skipped_reason not in fields
    assert d.apartment_match_confidence not in fields
    assert d.llm_latency_ms not in fields
    assert d.would_send_at not in fields


def test_create_draft_reply_route_skipped(test_client: AirtableClient) -> None:
    d = TEST.drafts
    tbl = MagicMock()
    tbl.create.return_value = {"id": "recDRAFT_3"}
    with patch.object(test_client, "_table", return_value=tbl):
        test_client.create_draft(
            **_draft_kwargs(
                reply_route="skipped",
                skipped_reason="no Reply-To header and parsed.email is None",
                recipient="",
            )
        )
    fields = tbl.create.call_args[0][0]
    assert fields[d.reply_route] == "skipped"
    assert fields[d.skipped_reason] == "no Reply-To header and parsed.email is None"
    assert fields[d.recipient] == ""


def test_create_draft_uses_test_schema_field_ids(test_client: AirtableClient) -> None:
    """Payload keys must be real TEST field IDs, not the 'MISSING' sentinel."""
    tbl = MagicMock()
    tbl.create.return_value = {"id": "recDRAFT_4"}
    with patch.object(test_client, "_table", return_value=tbl):
        test_client.create_draft(**_draft_kwargs())
    fields = tbl.create.call_args[0][0]
    assert "MISSING" not in fields
    # Every key should start with 'fld' (Airtable field ID format)
    for key in fields:
        assert key.startswith("fld"), f"Unexpected key in draft fields: {key!r}"


def test_create_draft_sender_field_id_is_correct(test_client: AirtableClient) -> None:
    """The sender field in the payload must use the known TEST field ID."""
    tbl = MagicMock()
    tbl.create.return_value = {"id": "recDRAFT_5"}
    with patch.object(test_client, "_table", return_value=tbl):
        test_client.create_draft(**_draft_kwargs(sender="fleisherautoreply@pearnyc.com"))
    fields = tbl.create.call_args[0][0]
    assert fields["fldYaTBUGIT10r9dj"] == "fleisherautoreply@pearnyc.com"


# ── find_or_create_inquiry ─────────────────────────────────────────────────────


def test_find_or_create_inquiry_returns_existing(
    client: AirtableClient, streeteasy_lead: ParsedLead
) -> None:
    existing = _record("recINQ_EXISTING", {PROD.inquiries.gmail_message_id_autoreply: "msg-abc"})
    with (
        patch.object(
            client, "find_inquiry_by_gmail_message_id", return_value=existing
        ) as mock_find,
        patch.object(client, "create_inquiry") as mock_create,
    ):
        result = client.find_or_create_inquiry(
            gmail_message_id="msg-abc",
            parsed=streeteasy_lead,
            apartment_record_id=None,
            user_record_id=None,
        )
    assert result == "recINQ_EXISTING"
    mock_find.assert_called_once_with("msg-abc")
    mock_create.assert_not_called()


def test_find_or_create_inquiry_creates_on_miss(
    client: AirtableClient, streeteasy_lead: ParsedLead
) -> None:
    with (
        patch.object(client, "find_inquiry_by_gmail_message_id", return_value=None),
        patch.object(client, "create_inquiry", return_value="recINQ_NEW") as mock_create,
    ):
        result = client.find_or_create_inquiry(
            gmail_message_id="msg-new",
            parsed=streeteasy_lead,
            apartment_record_id="recAPT1",
            user_record_id=None,
        )
    assert result == "recINQ_NEW"
    mock_create.assert_called_once_with(
        gmail_message_id="msg-new",
        parsed=streeteasy_lead,
        apartment_record_id="recAPT1",
        user_record_id=None,
    )
