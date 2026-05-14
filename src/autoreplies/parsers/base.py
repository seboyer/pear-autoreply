"""Shared parser types + source dispatcher.

Empirically-confirmed senders (PLAN.md "Empirical findings" + mbox analysis):
    - noreply@email.streeteasy.com           → StreetEasy
    - rentalclientservices@zillowrentals.com → Zillow / Trulia / HotPads
"""

import re
from dataclasses import dataclass
from email.message import Message
from typing import Literal

from bs4 import BeautifulSoup

Source = Literal["StreetEasy", "Zillow"]

_TITLE_PREFIXES = {"mr.", "mrs.", "ms.", "mx.", "dr.", "miss", "mr", "mrs", "ms", "mx", "dr"}


class ParserError(Exception):
    """Raised when no parser matches or required fields are missing."""


@dataclass(frozen=True)
class ParsedLead:
    source: Source
    # Prospect identity (Zillow has no first_name or phone)
    first_name: str | None
    last_name: str | None
    email: str | None              # prospect email (typically from Reply-To)
    phone: str | None
    # Listing info
    apartment_address: str | None  # raw, pre-normalization
    listing_url: str | None
    listing_id: str | None         # for StreetEasy only — Zillow URLs are opaque redirects
    # Body
    message_body: str | None
    # Provenance
    parser_used: str               # "streeteasy" | "zillow" | "llm_fallback"


SENDER_TO_SOURCE: dict[str, Source] = {
    "noreply@email.streeteasy.com": "StreetEasy",
    "rentalclientservices@zillowrentals.com": "Zillow",
}


def detect_source(message: Message) -> Source | None:
    """Return the canonical source for an inbound message, or None."""
    raw_from = (message.get("From") or "").lower()
    for sender, source in SENDER_TO_SOURCE.items():
        if sender in raw_from:
            return source
    return None


def parse(message: Message) -> ParsedLead:
    """Dispatch a message to the right parser.

    Raises ParserError if the source cannot be determined or required fields are missing.
    """
    # Local imports to avoid circular references.
    from . import streeteasy, zillow

    source = detect_source(message)
    if source == "StreetEasy":
        return streeteasy.parse(message)
    if source == "Zillow":
        return zillow.parse(message)
    raise ParserError(f"Unknown source for From: {message.get('From')!r}")


# ── Shared helpers ────────────────────────────────────────────────────────────

def get_body_part(message: Message, content_type: str) -> str | None:
    """Return the decoded body of the first part with `content_type`, or None.

    Walks multipart messages; falls back to the whole payload for non-multipart.
    """
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == content_type:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return None
    if message.get_content_type() == content_type:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = message.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return None


def html_to_text(html: str) -> str:
    """Flatten an HTML body to plaintext suitable for regex extraction.

    Drops <style>/<script> content, collapses runs of whitespace, normalises
    non-breaking spaces. Preserves text order.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["style", "script"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = text.replace(chr(0xa0), " ")  # NBSP -> regular space
    return re.sub(r"[ \t]+", " ", text)


def extract_reply_to_email(message: Message) -> str | None:
    """Return the bare email address from the Reply-To header, or None."""
    raw = message.get("Reply-To")
    if not raw:
        return None
    # Reply-To may be `Name <addr@example.com>` or just `addr@example.com`.
    match = re.search(r"[\w\.\+\-]+@[\w\.\-]+\.\w+", raw)
    return match.group(0) if match else None


def split_name(full: str | None) -> tuple[str | None, str | None]:
    """Split a display name into (first_name, last_name).

    - Strips common title prefixes (Mr./Mrs./Ms./Mx./Dr./Miss).
    - Single token after stripping → (None, that token). The name is too
      ambiguous to call it a first name; the template fallback handles it.
    - Two+ tokens → (first, " ".join(rest)).
    """
    if not full:
        return None, None
    tokens = full.strip().split()
    if tokens and tokens[0].lower() in _TITLE_PREFIXES:
        tokens = tokens[1:]
    if not tokens:
        return None, None
    if len(tokens) == 1:
        return None, tokens[0]
    return tokens[0], " ".join(tokens[1:])
