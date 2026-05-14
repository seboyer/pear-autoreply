"""Tests for parsers/base.py dispatcher + shared helpers."""

from __future__ import annotations

import email
from email import policy
from email.message import EmailMessage

import pytest

from autoreplies.parsers import parse
from autoreplies.parsers.base import (
    ParserError,
    detect_source,
    extract_reply_to_email,
    html_to_text,
    split_name,
)


def _msg(headers: str, body: str = "") -> EmailMessage:
    raw = (headers + "\r\n\r\n" + body).encode("utf-8")
    out = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(out, EmailMessage)
    return out


# ── detect_source ─────────────────────────────────────────────────────────────


def test_detect_source_streeteasy() -> None:
    msg = _msg("From: StreetEasy <noreply@email.streeteasy.com>\r\nSubject: x")
    assert detect_source(msg) == "StreetEasy"


def test_detect_source_zillow() -> None:
    msg = _msg(
        "From: Zillow Group Rentals <rentalclientservices@zillowrentals.com>\r\n"
        "Subject: x"
    )
    assert detect_source(msg) == "Zillow"


def test_detect_source_case_insensitive() -> None:
    msg = _msg("From: STREETEASY <NoReply@Email.StreetEasy.Com>\r\nSubject: x")
    assert detect_source(msg) == "StreetEasy"


def test_detect_source_unknown_returns_none() -> None:
    msg = _msg("From: random@example.com\r\nSubject: x")
    assert detect_source(msg) is None


# ── dispatcher ────────────────────────────────────────────────────────────────


def test_parse_unknown_source_raises() -> None:
    msg = _msg("From: random@example.com\r\nSubject: hello")
    with pytest.raises(ParserError, match="Unknown source"):
        parse(msg)


# ── split_name ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("full,expected", [
    ("Katie Shepherd", ("Katie", "Shepherd")),
    ("Kyra G. Cobb", ("Kyra", "G. Cobb")),
    ("Ms. Gray", (None, "Gray")),
    ("Mr. John Doe", ("John", "Doe")),
    ("Mx Avery Smith", ("Avery", "Smith")),
    ("Dr. Patel", (None, "Patel")),
    ("Madonna", (None, "Madonna")),
    ("", (None, None)),
    ("   ", (None, None)),
    (None, (None, None)),
])
def test_split_name(full: str | None, expected: tuple[str | None, str | None]) -> None:
    assert split_name(full) == expected


# ── extract_reply_to_email ────────────────────────────────────────────────────


def test_extract_reply_to_email_bare_address() -> None:
    msg = _msg("From: x@y.com\r\nReply-To: prospect@example.com\r\nSubject: x")
    assert extract_reply_to_email(msg) == "prospect@example.com"


def test_extract_reply_to_email_with_display_name() -> None:
    msg = _msg(
        "From: x@y.com\r\nReply-To: Jane Doe <jane@example.com>\r\nSubject: x"
    )
    assert extract_reply_to_email(msg) == "jane@example.com"


def test_extract_reply_to_email_missing_returns_none() -> None:
    msg = _msg("From: x@y.com\r\nSubject: x")
    assert extract_reply_to_email(msg) is None


# ── html_to_text ──────────────────────────────────────────────────────────────


def test_html_to_text_drops_style_and_script() -> None:
    html = (
        "<html><head><style>a {color: red}</style></head>"
        "<body><p>Hello <b>world</b></p>"
        "<script>alert(1)</script></body></html>"
    )
    text = html_to_text(html)
    assert "a {color" not in text
    assert "alert" not in text
    assert "Hello" in text and "world" in text


def test_html_to_text_normalises_nbsp_and_whitespace() -> None:
    html = "<p>foo&nbsp;&nbsp;&nbsp;bar</p>"
    text = html_to_text(html)
    # Triple-nbsp collapses to a single space.
    assert "foo bar" in text or "foo  bar" in text
    # No raw &nbsp; entity leaks through.
    assert "\xa0\xa0\xa0" not in text
