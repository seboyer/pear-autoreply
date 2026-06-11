"""Content+sender fingerprint dedupe for duplicate-message suppression (Phase 1)."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Protocol

logger = logging.getLogger(__name__)


class DuplicateLeadSuppressed(Exception):
    """Raised inside _phase_a when the lead is a re-send of an already-replied message.

    Caught by process_lead, which short-circuits the pipeline cleanly (no side effects).
    """

    def __init__(self, *, fingerprint: str, prior_message_id: str) -> None:
        self.fingerprint = fingerprint
        self.prior_message_id = prior_message_id
        super().__init__(f"duplicate of message_id={prior_message_id} (fp={fingerprint[:12]}…)")


_WS = re.compile(r"\s+")


def _norm(s: str | None) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def compute_fingerprint(
    *,
    mailbox: str,
    prospect_email: str | None,
    message_body: str | None,
    apartment_address: str | None,
    source: str,
) -> str:
    """Stable sha256 over the duplicate-identifying signal.

    INVARIANTS (load-bearing — do not change without re-reading DEDUPE_HANDOFF.md):
      - MUST include `prospect_email` AND be mailbox-scoped. The tour-variant
        message_body is ~3 near-constant strings ("Renter's Preferred Tour: In Person");
        without prospect+mailbox scoping you'd suppress nearly every tour request after
        the first.
      - Hash the PARSED, cleaned `message_body` (already normalized by the parser's
        _extract_message_body), NEVER the raw email body (raw carries per-send noise).
    """
    parts = [
        _norm(mailbox),
        _norm(prospect_email),
        _norm(message_body),
        _norm(apartment_address),
        _norm(source),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class DedupStore(Protocol):
    def recent_duplicate_message_id(
        self,
        *,
        mailbox: str,
        fingerprint: str,
        exclude_message_id: str,
        within_seconds: int,
        now: datetime | None = None,
    ) -> str | None: ...

    def record_reply(
        self,
        *,
        mailbox: str,
        fingerprint: str,
        gmail_message_id: str,
        inquiry_id: str | None,
        now: datetime | None = None,
    ) -> None: ...


class NoopDedup:
    """Default store: never suppresses, never records. Used when no dedup is wired
    (e.g. harness replay, or process_lead called without a dedup arg)."""

    def recent_duplicate_message_id(
        self,
        *,
        mailbox: str,
        fingerprint: str,
        exclude_message_id: str,
        within_seconds: int,
        now: datetime | None = None,
    ) -> str | None:
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
        return None
