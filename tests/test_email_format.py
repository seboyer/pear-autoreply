"""Tests for utils/email_format.py — plaintext→HTML reply rendering."""

from __future__ import annotations

from autoreplies.utils.email_format import plaintext_to_html


def test_newlines_become_br() -> None:
    out = plaintext_to_html("Hi there!\n\nThanks for your interest.")
    assert out == "Hi there!<br>\n<br>\nThanks for your interest."


def test_html_special_chars_escaped() -> None:
    out = plaintext_to_html("5 < 10 & rising > before")
    assert "&lt;" in out and "&amp;" in out and "&gt;" in out
    # No raw angle brackets except our own <br> tags.
    assert "<" not in out.replace("<br>", "")


def test_quotes_and_apostrophes_preserved() -> None:
    # quote=False keeps prose readable rather than emitting &#x27; / &quot;.
    out = plaintext_to_html('I\'m "glad" to help')
    assert "I'm" in out
    assert '"glad"' in out


def test_crlf_normalized() -> None:
    assert plaintext_to_html("a\r\nb") == "a<br>\nb"
    assert plaintext_to_html("a\rb") == "a<br>\nb"


def test_empty_string() -> None:
    assert plaintext_to_html("") == ""


def test_intentional_markdown_untouched() -> None:
    # No backslash/escaping logic here — bold markers pass through.
    assert plaintext_to_html("**bold**") == "**bold**"
