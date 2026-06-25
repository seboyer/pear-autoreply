"""Tests for utils/identifiers.py — email and phone normalization."""

from autoreplies.utils.identifiers import normalize_email, normalize_phone_e164

# ── normalize_email ───────────────────────────────────────────────────────────


def test_normalize_email_lowercases() -> None:
    assert normalize_email("Casey@Example.COM") == "casey@example.com"


def test_normalize_email_strips_whitespace() -> None:
    assert normalize_email("  user@example.com  ") == "user@example.com"


def test_normalize_email_none_returns_none() -> None:
    assert normalize_email(None) is None


def test_normalize_email_blank_returns_none() -> None:
    assert normalize_email("   ") is None


def test_normalize_email_empty_returns_none() -> None:
    assert normalize_email("") is None


# ── normalize_phone_e164 ──────────────────────────────────────────────────────


def test_normalize_phone_10_digit() -> None:
    assert normalize_phone_e164("6465550123") == "+16465550123"


def test_normalize_phone_none_returns_none() -> None:
    assert normalize_phone_e164(None) is None


def test_normalize_phone_empty_returns_none() -> None:
    assert normalize_phone_e164("") is None


def test_normalize_phone_9_digit_returns_none() -> None:
    assert normalize_phone_e164("646555012") is None


def test_normalize_phone_11_digit_returns_none() -> None:
    assert normalize_phone_e164("16465550123") is None


def test_normalize_phone_with_non_digit_returns_none() -> None:
    assert normalize_phone_e164("646-555-0123") is None


def test_normalize_phone_strips_whitespace() -> None:
    assert normalize_phone_e164("  6465550123  ") == "+16465550123"
