"""StreetEasy lead-email parser.

Empirically two body variants land at the agent mailbox (n~4k sample from the
legacy mbox, 2024-2026):

  - **Tour request** (~84%):
        "<name> Has Requested a Tour for <address>"
        "Renter's Preferred Tour: <In Person|Virtual>"
        — no prospect free-text.
  - **Question** (~14%):
        "You Received a Question About <address>"
        "<inline question text>"
        — prospect free-text follows the heading.

Both variants share the same contact block:
    "<name>  <email>  <mailto:...>  <phone>  <tel:+...>"
…and the canonical subject `<address> StreetEasy Inquiry From <name>`.

A small minority of emails on the sender allowlist are not leads at all —
listing-live confirmations, monthly newsletters, magic-code auth. The parser
asserts the canonical subject pattern and raises `ParserError` otherwise; the
upstream caller decides whether to LLM-fallback or drop.
"""

import re
from email.message import Message

from .base import (
    ParsedLead,
    ParserError,
    extract_reply_to_email,
    get_body_part,
    split_name,
)

SUBJECT_PATTERN = re.compile(
    r"^(?P<address>.+?)\s+StreetEasy Inquiry From\s+(?P<name>.+)$"
)

LISTING_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?streeteasy\.com/rental/(?P<id>\d+)",
    re.IGNORECASE,
)

# Phone lives behind a `tel:+1...` URI in the contact block — the most reliable
# anchor since SE bodies also embed area-code-shaped strings in disclosures.
_TEL_URI_PATTERN = re.compile(r"tel:\+?(\d{6,15})")

# Tour variant — capture the preference for the analytics surface.
_TOUR_PREFERENCE_PATTERN = re.compile(
    r"Renter's\s+Preferred\s+Tour:\s*(?P<preference>[^\n\r]+?)\s*(?:\n|$)",
    re.IGNORECASE,
)

# Question variant — the prospect's free text sits between the heading line and
# the contact block. The contact block always starts with the prospect's name
# repeated, followed by their email and a `mailto:` URI.
_QUESTION_HEADING_PATTERN = re.compile(
    r"You\s+Received\s+a\s+Question\s+About\s+[^\n]+\n+(?P<body>.+?)(?=\n\s*\S+@\S+\s|\nmailto:|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse(message: Message) -> ParsedLead:
    """Parse a StreetEasy lead email into a `ParsedLead`."""
    subject = (message.get("Subject") or "").strip()
    subject_match = SUBJECT_PATTERN.match(subject)
    if not subject_match:
        raise ParserError(
            f"StreetEasy subject does not match expected pattern: {subject!r}"
        )

    raw_address = subject_match.group("address").strip()
    raw_name = subject_match.group("name").strip()
    first_name, last_name = split_name(raw_name)

    # Plain text part is the simplest extraction surface for SE — the HTML
    # equivalent says the same thing but adds noise from layout tables.
    body = get_body_part(message, "text/plain") or ""
    if not body:
        # Some early SE messages carry only HTML. Fall back to the HTML part.
        html = get_body_part(message, "text/html") or ""
        if html:
            from .base import html_to_text
            body = html_to_text(html)

    email_addr = extract_reply_to_email(message)
    phone = _extract_phone(body)
    listing_url, listing_id = _extract_listing(body)
    message_body = _extract_message_body(body)

    return ParsedLead(
        source="StreetEasy",
        first_name=first_name,
        last_name=last_name,
        email=email_addr,
        phone=phone,
        apartment_address=raw_address,
        listing_url=listing_url,
        listing_id=listing_id,
        message_body=message_body,
        parser_used="streeteasy",
    )


def _extract_phone(body: str) -> str | None:
    match = _TEL_URI_PATTERN.search(body)
    if not match:
        return None
    digits = match.group(1)
    # Strip a leading country-code 1 for US/CA numbers so downstream matching
    # against Users.Phone (which is typically stored without it) is consistent.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _extract_listing(body: str) -> tuple[str | None, str | None]:
    match = LISTING_URL_PATTERN.search(body)
    if not match:
        return None, None
    return match.group(0), match.group("id")


def _extract_message_body(body: str) -> str | None:
    """Return the prospect-facing free-text portion of the body.

    - Tour variant: returns the preference (e.g. "Renter's Preferred Tour: In Person")
      so the analytics surface always shows what the prospect did.
    - Question variant: returns the inline question text.
    - Returns None when neither pattern matches (defensive — surface the email
      structurally, don't guess).
    """
    if q := _QUESTION_HEADING_PATTERN.search(body):
        return _clean_message(q.group("body"))
    if t := _TOUR_PREFERENCE_PATTERN.search(body):
        return f"Renter's Preferred Tour: {t.group('preference').strip()}"
    return None


def _clean_message(raw: str) -> str:
    """Trim whitespace, drop SE markup leftovers, collapse blank runs."""
    text = re.sub(r"<[^>]+>", "", raw)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_listing_id(body: str) -> str | None:
    """Pull the numeric StreetEasy listing ID from anywhere in the body."""
    if match := LISTING_URL_PATTERN.search(body):
        return match.group("id")
    return None
