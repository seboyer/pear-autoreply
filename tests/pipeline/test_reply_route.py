"""Tests for pipeline/reply_route.py."""

from email.message import Message

from autoreplies.parsers.base import ParsedLead
from autoreplies.pipeline.reply_route import resolve_reply_destination, subject_for_reply

# ── Helpers ───────────────────────────────────────────────────────────────────


def _msg(*, reply_to: str | None = None, message_id: str | None = None) -> Message:
    """Build a minimal email.message.Message for testing."""
    m = Message()
    if reply_to:
        m["Reply-To"] = reply_to
    if message_id:
        m["Message-ID"] = message_id
    return m


def _parsed(*, email: str | None = "prospect@example.com") -> ParsedLead:
    return ParsedLead(
        source="StreetEasy",
        first_name="Jane",
        last_name="Doe",
        email=email,
        phone=None,
        apartment_address="123 Main St",
        listing_url=None,
        listing_id=None,
        message_body=None,
        parser_used="streeteasy",
    )


# ── resolve_reply_destination ─────────────────────────────────────────────────


def test_thread_route_when_reply_to_and_thread_id() -> None:
    msg = _msg(reply_to="prospect@example.com", message_id="<abc@se>")
    dest = resolve_reply_destination(message=msg, parsed=_parsed(), thread_id="thread-xyz")

    assert dest.route == "thread"
    assert dest.recipient == "prospect@example.com"
    assert dest.skipped_reason is None
    assert dest.thread_id == "thread-xyz"
    assert dest.in_reply_to_message_id == "<abc@se>"


def test_direct_route_when_no_thread_id() -> None:
    msg = _msg(reply_to="prospect@example.com", message_id="<abc@se>")
    dest = resolve_reply_destination(message=msg, parsed=_parsed(), thread_id=None)

    assert dest.route == "direct"
    assert dest.recipient == "prospect@example.com"
    assert dest.skipped_reason is None


def test_skipped_when_no_reply_to_and_no_parsed_email() -> None:
    msg = _msg()
    dest = resolve_reply_destination(message=msg, parsed=_parsed(email=None), thread_id="t")

    assert dest.route == "skipped"
    assert dest.recipient is None
    assert dest.skipped_reason is not None
    assert "no Reply-To" in dest.skipped_reason


def test_prefers_reply_to_over_parsed_email() -> None:
    msg = _msg(reply_to="reply-to@example.com")
    dest = resolve_reply_destination(
        message=msg, parsed=_parsed(email="parsed@example.com"), thread_id="t"
    )
    assert dest.recipient == "reply-to@example.com"


def test_falls_back_to_parsed_email_when_no_reply_to_header() -> None:
    msg = _msg()  # no Reply-To
    dest = resolve_reply_destination(
        message=msg, parsed=_parsed(email="parsed@example.com"), thread_id="t"
    )
    assert dest.route == "thread"
    assert dest.recipient == "parsed@example.com"


def test_in_reply_to_is_none_when_no_message_id_header() -> None:
    msg = _msg(reply_to="x@y.com")  # no Message-ID
    dest = resolve_reply_destination(message=msg, parsed=_parsed(), thread_id="t")
    assert dest.in_reply_to_message_id is None


def test_thread_id_propagated_even_on_skipped_route() -> None:
    """thread_id is preserved on skipped destinations for diagnostic purposes."""
    msg = _msg()
    dest = resolve_reply_destination(message=msg, parsed=_parsed(email=None), thread_id="t-99")
    assert dest.route == "skipped"
    assert dest.thread_id == "t-99"


def test_empty_thread_id_string_treated_as_no_thread() -> None:
    msg = _msg(reply_to="x@y.com")
    dest = resolve_reply_destination(message=msg, parsed=_parsed(), thread_id="")
    assert dest.route == "direct"


# ── subject_for_reply ─────────────────────────────────────────────────────────


def test_subject_for_reply_adds_re_prefix() -> None:
    assert subject_for_reply("65 Saint Marks Ave StreetEasy Inquiry From Jane") == (
        "Re: 65 Saint Marks Ave StreetEasy Inquiry From Jane"
    )


def test_subject_for_reply_idempotent_lowercase() -> None:
    assert subject_for_reply("re: Already a reply") == "re: Already a reply"


def test_subject_for_reply_idempotent_uppercase() -> None:
    assert subject_for_reply("RE: Already a reply") == "RE: Already a reply"


def test_subject_for_reply_idempotent_mixed_case() -> None:
    assert subject_for_reply("Re: Foo") == "Re: Foo"


def test_subject_for_reply_empty_string() -> None:
    assert subject_for_reply("") == "Re: "


def test_subject_for_reply_whitespace_only() -> None:
    assert subject_for_reply("   ") == "Re:    "
