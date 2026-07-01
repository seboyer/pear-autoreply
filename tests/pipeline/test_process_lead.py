"""Fixture-driven end-to-end tests for pipeline/process_lead.py.

Tests verify that process_lead, when wired with harness strategies, drives the
full parse → match → template → draft flow and materialises the right Drafts row.
Real .eml fixtures from fixtures/anonymized/ are used so the parsers run against
actual email bytes — only the downstream Airtable/LLM calls are mocked.
"""

from __future__ import annotations

import email as email_lib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from autoreplies.harness.pipeline import build_harness_strategies
from autoreplies.pipeline.process_lead import process_lead
from autoreplies.pipeline.strategies import PipelineStrategies
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import PROD, TEST

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
    # Both lookup variants return the same agent so this mock works regardless
    # of which agent_lookup_by the caller passes to process_lead.
    at.find_monitored_user_by_leads_email.return_value = agent_record
    at.find_monitored_user_by_autoreply_email.return_value = agent_record

    # Apartment matching. The structured matcher returns (record, score); the
    # streeteasy_id matcher returns just record.
    at.match_apartment_by_streeteasy_id.return_value = apartment_record
    at.match_apartment_by_address.return_value = (
        (apartment_record, 95) if apartment_record else None
    )

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


# ── Production strategies (Live*) delegate correctly ─────────────────────────


def test_production_strategies_wire_live_types() -> None:
    """build_production_strategies returns a PipelineStrategies with Live* instances."""
    from unittest.mock import MagicMock

    from autoreplies.pipeline.strategies import (
        LiveSend,
        LiveSlack,
        LiveSupabase,
        build_production_strategies,
    )

    result = build_production_strategies(
        queue=MagicMock(), slack_client=MagicMock(), supabase_client=MagicMock()
    )
    assert isinstance(result.send, LiveSend)
    assert isinstance(result.slack, LiveSlack)
    assert isinstance(result.supabase, LiveSupabase)


# ── No services → raises NotImplementedError ─────────────────────────────────


def test_no_services_raises_not_implemented() -> None:
    """When no services are passed (production path), phase A raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        process_lead("msg-id", "agent@pearnyc.com")


# ── FakeDedup — in-memory DedupStore for integration tests ────────────────────


class FakeDedup:
    """In-memory DedupStore that faithfully honours exclude_message_id."""

    def __init__(self) -> None:
        # List of (mailbox, fingerprint, gmail_message_id, inquiry_id, replied_at).
        self._records: list[tuple[str, str, str, str | None, datetime]] = []
        # List of (person_id, mailbox, gmail_message_id, replied_at).
        self._person_records: list[tuple[str, str, str, datetime]] = []

    def recent_duplicate_message_id(
        self,
        *,
        mailbox: str,
        fingerprint: str,
        exclude_message_id: str,
        within_seconds: int,
        now: datetime | None = None,
    ) -> str | None:
        now = now or datetime.now(UTC)
        from datetime import timedelta

        cutoff = now - timedelta(seconds=within_seconds)
        for mb, fp, mid, _inq, replied_at in reversed(self._records):
            if (
                mb == mailbox
                and fp == fingerprint
                and mid != exclude_message_id
                and replied_at >= cutoff
            ):
                return mid
        return None

    def record_reply(
        self,
        *,
        mailbox: str,
        fingerprint: str,
        gmail_message_id: str,
        inquiry_id: str | None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        self._records.append((mailbox, fingerprint, gmail_message_id, inquiry_id, now))

    def recent_person_reply(
        self,
        *,
        person_id: str,
        mailbox: str,
        exclude_message_id: str,
        within_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        from datetime import timedelta

        cutoff = now - timedelta(seconds=within_seconds)
        for pid, mb, mid, replied_at in reversed(self._person_records):
            if (
                pid == person_id
                and mb == mailbox
                and mid != exclude_message_id
                and replied_at >= cutoff
            ):
                return True
        return False

    def record_person_reply(
        self,
        *,
        person_id: str,
        mailbox: str,
        gmail_message_id: str,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        self._person_records.append((person_id, mailbox, gmail_message_id, now))


class FakeResolver:
    """In-memory PersonResolver returning a fixed person_id."""

    def __init__(self, person_id: str | None = "person-fixed-id") -> None:
        self._person_id = person_id
        self.calls: list[dict[str, Any]] = []

    def resolve_person_id(self, *, email: str | None, phone: str | None) -> str | None:
        self.calls.append({"email": email, "phone": phone})
        return self._person_id


# ── Dedup integration tests ───────────────────────────────────────────────────


def test_duplicate_suppressed_second_message_not_drafted() -> None:
    """Two messages with the same content fingerprint: only the first creates a draft."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    airtable = _mock_airtable(inquiry_id="recINQ_SE")
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)
    fake_dedup = FakeDedup()

    # First message — should create a draft and record fingerprint.
    process_lead(
        "gmail-msg-first",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail(fixture, thread_id="thread-se-001"),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
    )
    assert airtable.create_draft.call_count == 1

    # Second message — same fixture = same fingerprint, different message_id.
    process_lead(
        "gmail-msg-second",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail(fixture, thread_id="thread-se-001"),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
    )
    # create_draft must NOT have been called again.
    assert airtable.create_draft.call_count == 1


def test_different_content_both_send() -> None:
    """Two messages with different content fingerprints both create a draft."""
    airtable = _mock_airtable()
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)
    fake_dedup = FakeDedup()

    process_lead(
        "gmail-msg-se",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail("streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
    )
    process_lead(
        "gmail-msg-zl",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail("zillow/lead__170-prospect-pl-3b__1.eml"),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
    )
    assert airtable.create_draft.call_count == 2


def test_fail_open_when_dedup_raises() -> None:
    """If DedupStore.recent_duplicate_message_id raises, the reply still happens."""

    class BrokenDedup:
        def recent_duplicate_message_id(self, **_: object) -> str | None:
            raise RuntimeError("database exploded")

        def record_reply(self, **_: object) -> None:
            return None

    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    airtable = _mock_airtable()
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)

    # Must NOT raise; must still create the draft.
    process_lead(
        "gmail-msg-broken-dedup",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
        dedup=BrokenDedup(),  # type: ignore[arg-type]
    )
    airtable.create_draft.assert_called_once()


def _mock_airtable_prod(
    *,
    agent_repeat_template: str = "",
    inquiry_id: str = "recINQ_PROD",
) -> AirtableClient:
    """AirtableClient mock wired with PROD schema (for Phase-2 repeat template tests)."""
    at = MagicMock(spec=AirtableClient)
    at.schema = PROD

    agent_record = {
        "id": "recAGENT_PROD",
        "fields": {
            PROD.users.name: "Garland Agent",
            PROD.users.email: "garland@pearnyc.com",
            PROD.users.autoreply_template: "",  # empty → uses pear fallback for first-touch
            PROD.users.autoreply_repeat_template: agent_repeat_template,
        },
    }
    at.find_monitored_user_by_leads_email.return_value = agent_record
    at.find_monitored_user_by_autoreply_email.return_value = agent_record
    at.match_apartment_by_streeteasy_id.return_value = None
    at.match_apartment_by_address.return_value = None
    at.find_existing_user.return_value = None
    at.find_inquiry_by_gmail_message_id.return_value = None
    at.find_or_create_inquiry.return_value = inquiry_id
    at.create_draft.return_value = "recDRAFT_PROD"
    return at


def test_skipped_route_does_not_record_fingerprint() -> None:
    """When route=skipped, record_reply is NOT called."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    raw = _load_fixture_bytes(fixture)
    msg = email_lib.message_from_bytes(raw)
    del msg["Reply-To"]

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

    fake_dedup = FakeDedup()

    with patch("autoreplies.pipeline.process_lead.parsers_base.parse", return_value=mock_parsed):
        process_lead(
            "gmail-msg-skip",
            "garland@pearnyc.com",
            strategies=_harness_strategies(airtable),
            gmail=gmail,
            airtable=airtable,
            llm=llm,
            dedup=fake_dedup,
        )

    # Route is skipped — no fingerprint should be recorded.
    assert len(fake_dedup._records) == 0


# ── Phase 2: person-keyed repeat-inquiry detection ────────────────────────────


def test_phase2_first_inquiry_uses_first_touch_template_and_records_person() -> None:
    """First inquiry from a known person: first-touch template, person_id recorded."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    airtable = _mock_airtable(inquiry_id="recINQ_FIRST")
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)
    fake_dedup = FakeDedup()
    resolver = FakeResolver(person_id="person-abc")

    process_lead(
        "gmail-msg-first",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
        person_resolver=resolver,
        repeat_inquiry_mode="enforce",
    )

    airtable.create_draft.assert_called_once()
    # First inquiry: first-touch template was used (not repeat).
    template_text = llm.fill_template.call_args.kwargs["template_text"]
    from autoreplies.services.templates import (
        get_pear_fallback_template,
        get_pear_repeat_fallback_template,
    )

    assert template_text == get_pear_fallback_template()
    assert template_text != get_pear_repeat_fallback_template()
    # person_id was recorded.
    assert len(fake_dedup._person_records) == 1
    assert fake_dedup._person_records[0][0] == "person-abc"


def test_phase2_repeat_inquiry_same_person_same_mailbox_uses_repeat_template() -> None:
    """2nd inquiry from same person+mailbox in window with enforce: repeat template used.

    Uses PROD schema (autoreply_repeat_template has a real field ID there).
    Pre-seeds the person record to avoid Phase-1 fingerprint suppression from
    running two calls with the same fixture.
    """
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    repeat_text = "Thanks for reaching out again, {{first_name|there}}!"
    airtable = _mock_airtable_prod(agent_repeat_template=repeat_text)
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)
    fake_dedup = FakeDedup()
    resolver = FakeResolver(person_id="person-repeat")

    # Pre-seed a prior person reply so the "second" message is detected as a repeat.
    fake_dedup._person_records.append(
        ("person-repeat", "garland@pearnyc.com", "prior-msg", datetime.now(UTC))
    )

    # "Second" message — person has a prior reply in window → is_repeat=True.
    process_lead(
        "gmail-msg-second",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
        person_resolver=resolver,
        repeat_inquiry_mode="enforce",
    )

    airtable.create_draft.assert_called_once()
    second_template_text = llm.fill_template.call_args.kwargs["template_text"]
    assert second_template_text == repeat_text


def test_phase2_repeat_same_person_different_mailbox_uses_first_touch() -> None:
    """2nd inquiry from same person but DIFFERENT mailbox: first-touch template used.

    Pre-seeds a person reply on garland@. Runs a message on jair@ (a different
    mailbox). The person dedup is scoped to (person_id, mailbox), so jair@ should
    not see it as a repeat.
    """
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    fake_dedup = FakeDedup()
    resolver = FakeResolver(person_id="person-cross")

    # Pre-seed: person replied from mailbox A (garland@).
    fake_dedup._person_records.append(
        ("person-cross", "garland@pearnyc.com", "prior-msg", datetime.now(UTC))
    )

    # Run on mailbox B (jair@) — different mailbox, should NOT be a repeat.
    # Patch _addressed_to_mailbox so the Hiver check passes for jair@.
    airtable2 = _mock_airtable(inquiry_id="recINQ_DIFF2")
    llm = _mock_llm()
    with patch("autoreplies.pipeline.process_lead._addressed_to_mailbox", return_value=True):
        process_lead(
            "gmail-msg-second",
            "jair@pearnyc.com",
            strategies=_harness_strategies(airtable2),
            gmail=_mock_gmail(fixture),
            airtable=airtable2,
            llm=llm,
            dedup=fake_dedup,
            person_resolver=resolver,
            repeat_inquiry_mode="enforce",
        )

    second_template_text = llm.fill_template.call_args.kwargs["template_text"]
    from autoreplies.services.templates import get_pear_fallback_template

    assert second_template_text == get_pear_fallback_template()


def test_phase2_empty_agent_repeat_field_uses_pear_repeat_fallback() -> None:
    """Repeat inquiry with blank agent repeat field → Pear repeat fallback (not first-touch).

    Uses PROD schema with an empty repeat template and a pre-seeded person record.
    """
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    # PROD schema, empty repeat template.
    airtable = _mock_airtable_prod(agent_repeat_template="")
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)
    fake_dedup = FakeDedup()
    resolver = FakeResolver(person_id="person-fallback")

    # Pre-seed prior person reply.
    fake_dedup._person_records.append(
        ("person-fallback", "garland@pearnyc.com", "prior-msg", datetime.now(UTC))
    )

    # "Second" message — should use pear repeat fallback (agent field is empty).
    process_lead(
        "gmail-msg-second",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
        person_resolver=resolver,
        repeat_inquiry_mode="enforce",
    )

    second_template_text = llm.fill_template.call_args.kwargs["template_text"]
    from autoreplies.services.templates import (
        get_pear_fallback_template,
        get_pear_repeat_fallback_template,
    )

    assert second_template_text == get_pear_repeat_fallback_template()
    assert second_template_text != get_pear_fallback_template()


def test_phase2_resolver_none_returns_fails_open_to_first_touch() -> None:
    """Resolver returns None (unknown person) → first-touch reply, no person recorded."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    airtable = _mock_airtable()
    llm = _mock_llm()
    fake_dedup = FakeDedup()
    resolver = FakeResolver(person_id=None)

    process_lead(
        "gmail-msg-unknown",
        "garland@pearnyc.com",
        strategies=_harness_strategies(airtable),
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
        person_resolver=resolver,
        repeat_inquiry_mode="enforce",
    )

    airtable.create_draft.assert_called_once()
    # No person record written (person_id was None).
    assert len(fake_dedup._person_records) == 0


def test_phase2_resolver_raises_fails_open_to_first_touch() -> None:
    """Resolver raises → first-touch reply still happens (fail-open)."""

    class ExplodingResolver:
        def resolve_person_id(self, *, email: str | None, phone: str | None) -> str | None:
            raise RuntimeError("resolver exploded")

    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    airtable = _mock_airtable()
    llm = _mock_llm()

    process_lead(
        "gmail-msg-explode",
        "garland@pearnyc.com",
        strategies=_harness_strategies(airtable),
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
        person_resolver=ExplodingResolver(),  # type: ignore[arg-type]
        repeat_inquiry_mode="enforce",
    )

    airtable.create_draft.assert_called_once()


def test_phase2_observe_mode_logs_but_sends_first_touch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """mode=observe: logs would-be-repeat, but sends first-touch template.

    Pre-seeds the person record to avoid Phase-1 fingerprint suppression.
    """
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    airtable = _mock_airtable()
    llm = _mock_llm()
    fake_dedup = FakeDedup()
    resolver = FakeResolver(person_id="person-observe")

    # Pre-seed prior person reply so "second" message is detected as a repeat.
    fake_dedup._person_records.append(
        ("person-observe", "garland@pearnyc.com", "prior-msg", datetime.now(UTC))
    )

    # "Second" message — observe mode: should log but send first-touch.
    with caplog.at_level("INFO", logger="autoreplies.pipeline.process_lead"):
        process_lead(
            "gmail-msg-second",
            "garland@pearnyc.com",
            strategies=_harness_strategies(airtable),
            gmail=_mock_gmail(fixture),
            airtable=airtable,
            llm=llm,
            dedup=fake_dedup,
            person_resolver=resolver,
            repeat_inquiry_mode="observe",
        )

    assert any("OBSERVE repeat inquiry" in r.message for r in caplog.records)
    second_template_text = llm.fill_template.call_args.kwargs["template_text"]
    from autoreplies.services.templates import get_pear_fallback_template

    assert second_template_text == get_pear_fallback_template()


def test_phase2_off_mode_does_not_call_resolver() -> None:
    """mode=off: resolver is never called, no person record written."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    airtable = _mock_airtable()
    llm = _mock_llm()
    fake_dedup = FakeDedup()
    resolver = FakeResolver(person_id="person-should-not-be-called")

    process_lead(
        "gmail-msg-off",
        "garland@pearnyc.com",
        strategies=_harness_strategies(airtable),
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
        dedup=fake_dedup,
        person_resolver=resolver,
        repeat_inquiry_mode="off",
    )

    # Resolver must not have been called at all.
    assert len(resolver.calls) == 0
    # No person records written.
    assert len(fake_dedup._person_records) == 0


# ── RFC-822 Message-ID extraction (Part A: cross-system stitching) ─────────────


def test_phase_a_extracts_rfc822_message_id_and_passes_to_supabase() -> None:
    """The inbound RFC-822 Message-ID header is extracted (raw form, brackets kept)
    and passed to the Supabase upsert. The SupabaseClient flag gates whether it's
    actually written, so process_lead always forwards it."""
    fixture = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"
    expected = (
        email_lib.message_from_bytes(_load_fixture_bytes(fixture)).get("Message-ID") or ""
    ).strip()
    assert expected  # fixture sanity: it carries a Message-ID header

    airtable = _mock_airtable(inquiry_id="recINQ_RFC")
    llm = _mock_llm()
    strategies = _harness_strategies(airtable)
    strategies.supabase = MagicMock()  # capture the upsert kwargs

    process_lead(
        "gmail-msg-rfc",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=_mock_gmail(fixture),
        airtable=airtable,
        llm=llm,
    )

    strategies.supabase.upsert_inquiry.assert_called_once()
    assert strategies.supabase.upsert_inquiry.call_args.kwargs["rfc822_message_id"] == expected
