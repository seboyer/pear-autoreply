"""Shared fixture loading for parser tests."""

from __future__ import annotations

import email
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "anonymized"


def load_fixture(relative_path: str) -> EmailMessage:
    """Load a `.eml` fixture by path relative to `fixtures/anonymized/`."""
    path = FIXTURES_DIR / relative_path
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES_DIR
