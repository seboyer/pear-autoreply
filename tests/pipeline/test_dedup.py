"""Tests for pipeline/dedup.py — compute_fingerprint and NoopDedup."""

from __future__ import annotations

from autoreplies.pipeline.dedup import (
    NoopDedup,
    compute_fingerprint,
)

# ── compute_fingerprint purity ────────────────────────────────────────────────


def test_identical_inputs_produce_identical_fingerprint() -> None:
    fp1 = compute_fingerprint(
        mailbox="agent@pearnyc.com",
        prospect_email="prospect@example.com",
        message_body="Hi, I'd like to tour the apartment.",
        apartment_address="65 Saint Mark's Avenue #2B",
        source="StreetEasy",
    )
    fp2 = compute_fingerprint(
        mailbox="agent@pearnyc.com",
        prospect_email="prospect@example.com",
        message_body="Hi, I'd like to tour the apartment.",
        apartment_address="65 Saint Mark's Avenue #2B",
        source="StreetEasy",
    )
    assert fp1 == fp2


def test_different_message_body_produces_different_fingerprint() -> None:
    base = dict(
        mailbox="agent@pearnyc.com",
        prospect_email="prospect@example.com",
        apartment_address="65 Saint Mark's Avenue #2B",
        source="StreetEasy",
    )
    fp1 = compute_fingerprint(**base, message_body="I'd like a 1-bedroom.")  # type: ignore[arg-type]
    fp2 = compute_fingerprint(**base, message_body="Can I see the 2-bedroom?")  # type: ignore[arg-type]
    assert fp1 != fp2


def test_different_prospect_email_produces_different_fingerprint() -> None:
    base = dict(
        mailbox="agent@pearnyc.com",
        message_body="Hi, interested in the apartment.",
        apartment_address="65 Saint Mark's Avenue #2B",
        source="StreetEasy",
    )
    fp1 = compute_fingerprint(**base, prospect_email="alice@example.com")  # type: ignore[arg-type]
    fp2 = compute_fingerprint(**base, prospect_email="bob@example.com")  # type: ignore[arg-type]
    assert fp1 != fp2


def test_different_mailbox_produces_different_fingerprint() -> None:
    base = dict(
        prospect_email="prospect@example.com",
        message_body="Tour request",
        apartment_address="65 Saint Mark's Avenue #2B",
        source="StreetEasy",
    )
    fp1 = compute_fingerprint(**base, mailbox="agent1@pearnyc.com")  # type: ignore[arg-type]
    fp2 = compute_fingerprint(**base, mailbox="agent2@pearnyc.com")  # type: ignore[arg-type]
    assert fp1 != fp2


def test_whitespace_normalisation() -> None:
    """Extra whitespace and case differences collapse to the same fingerprint."""
    fp1 = compute_fingerprint(
        mailbox="AGENT@PEARNYC.COM",
        prospect_email="  Prospect@Example.COM  ",
        message_body="  Hi There  ",
        apartment_address="65  Saint Mark's Avenue  #2B",
        source="StreetEasy",
    )
    fp2 = compute_fingerprint(
        mailbox="agent@pearnyc.com",
        prospect_email="prospect@example.com",
        message_body="hi there",
        apartment_address="65 saint mark's avenue #2b",
        source="streeteasy",
    )
    assert fp1 == fp2


def test_none_fields_handled() -> None:
    """None values are treated as empty strings — no TypeError."""
    fp = compute_fingerprint(
        mailbox="agent@pearnyc.com",
        prospect_email=None,
        message_body=None,
        apartment_address=None,
        source="Zillow",
    )
    assert isinstance(fp, str)
    assert len(fp) == 64  # sha256 hex digest


# ── NoopDedup ─────────────────────────────────────────────────────────────────


def test_noop_dedup_returns_none() -> None:
    nd = NoopDedup()
    result = nd.recent_duplicate_message_id(
        mailbox="agent@pearnyc.com",
        fingerprint="abc123",
        exclude_message_id="msg-1",
        within_seconds=3600,
    )
    assert result is None


def test_noop_dedup_record_reply_no_ops() -> None:
    nd = NoopDedup()
    # Must not raise.
    nd.record_reply(
        mailbox="agent@pearnyc.com",
        fingerprint="abc123",
        gmail_message_id="msg-1",
        inquiry_id="recINQ1",
    )
