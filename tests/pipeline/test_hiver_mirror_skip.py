"""Tests for the Hiver shared-inbox mirror gate in process_lead.

Hiver copies every shared-inbox message into each Hiver user's personal mailbox
with the original recipients intact, so a mailbox can hold mail it was never
addressed on. process_lead must only act on a message actually addressed to the
mailbox being polled — otherwise one shared-inbox lead is auto-replied once per
monitored Hiver user.
"""

from __future__ import annotations

import email
from email.message import Message
from unittest.mock import MagicMock

from autoreplies.pipeline.process_lead import _addressed_to_mailbox, process_lead


def _msg(headers: str) -> Message:
    # Compat32 policy, matching GmailClient.get_message (email.message_from_bytes).
    return email.message_from_bytes((headers + "\r\n\r\n").encode())


# ── _addressed_to_mailbox ─────────────────────────────────────────────────────


def test_addressed_to_mailbox_matches_to() -> None:
    assert _addressed_to_mailbox(_msg("To: inbox@pearnyc.com"), "inbox@pearnyc.com") is True


def test_addressed_to_mailbox_mismatch_is_false() -> None:
    # Hiver mirror: addressed to the shared inbox, sitting in jair@'s mailbox.
    assert _addressed_to_mailbox(_msg("To: inbox@pearnyc.com"), "jair@pearnyc.com") is False


def test_addressed_to_mailbox_is_case_insensitive() -> None:
    assert _addressed_to_mailbox(_msg("To: Inbox@PearNYC.com"), "inbox@pearnyc.com") is True


def test_addressed_to_mailbox_handles_display_name() -> None:
    m = _msg("To: Richard Garland <inbox@pearnyc.com>")
    assert _addressed_to_mailbox(m, "inbox@pearnyc.com") is True


def test_addressed_to_mailbox_checks_cc() -> None:
    m = _msg("To: someone@else.com\r\nCc: jair@pearnyc.com")
    assert _addressed_to_mailbox(m, "jair@pearnyc.com") is True


def test_addressed_to_mailbox_no_recipients_is_false() -> None:
    assert _addressed_to_mailbox(_msg("Subject: x"), "inbox@pearnyc.com") is False


# ── process_lead skip ─────────────────────────────────────────────────────────


def test_process_lead_skips_hiver_mirror() -> None:
    """Message addressed to inbox@ but polled from jair@ (Hiver mirror) is
    skipped: no reply, no Airtable inquiry, no Slack, and no exception raised."""
    mirror = _msg(
        "From: StreetEasy <noreply@email.streeteasy.com>\r\n"
        "To: inbox@pearnyc.com\r\n"
        "Reply-To: prospect@example.com\r\n"
        "Subject: 524 Lafayette Avenue #2 StreetEasy Inquiry From Valentina Crespo"
    )
    gmail = MagicMock()
    gmail.get_message.return_value = (mirror, "thread-1")
    airtable = MagicMock()
    llm = MagicMock()
    strategies = MagicMock()

    # Polling jair@, but the message is addressed to inbox@ → must be skipped.
    process_lead(
        "gmail-msg-mirror",
        "jair@pearnyc.com",
        strategies=strategies,
        gmail=gmail,
        airtable=airtable,
        llm=llm,
    )

    gmail.get_message.assert_called_once_with("gmail-msg-mirror")
    airtable.find_or_create_inquiry.assert_not_called()
    strategies.send.send_reply.assert_not_called()
    strategies.supabase.upsert_inquiry.assert_not_called()
    strategies.slack.post_lead.assert_not_called()
