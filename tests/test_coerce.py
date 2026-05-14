"""Tests for utils.coerce — covers parity with the legacy Zapier script."""

import pytest

from autoreplies.utils.coerce import to_date_or_null, to_null, to_number_or_null


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("hello", "hello"),
        (0, 0),
        (False, False),
    ],
)
def test_to_null(value: object, expected: object) -> None:
    assert to_null(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("$1,200", 1200.0),
        ("1234", 1234.0),
        ("3.14", 3.14),
        ("not a number", None),
        ("$0", 0.0),
    ],
)
def test_to_number_or_null(value: object, expected: float | None) -> None:
    assert to_number_or_null(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("2026-04-25", "2026-04-25"),
        ("2026-04-25T12:00:00Z", "2026-04-25T12:00:00Z"),
    ],
)
def test_to_date_or_null(value: object, expected: str | None) -> None:
    assert to_date_or_null(value) == expected
