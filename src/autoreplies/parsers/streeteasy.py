"""StreetEasy lead-email parser.

Empirically three body/subject variants land at the agent mailbox:

  - **Tour request** (~84% of canonical leads):
        Subject: "<address> StreetEasy Inquiry From <name>"
        Body heading: "<name> Has Requested a Tour for <address>"
        "Renter's Preferred Tour: <In Person|Virtual>"
        — no prospect free-text.
  - **Question** (~14% of canonical leads):
        Subject: "<address> StreetEasy Inquiry From <name>"
        Body heading: "You Received a Question About <address>"
        "<inline question text>"
        — prospect free-text follows the heading.
  - **New Message From** (proactive prospect outreach):
        Subject: "New Message From <name>"
        Body heading: "You Have a New Message"
        "<prospect free-text>"
        — listing-agnostic; apartment_address is None for these leads.

The canonical variants share the contact block:
    "<name>  <email>  <mailto:...>  <phone>  <tel:+...>"
…and the subject `<address> StreetEasy Inquiry From <name>`.

Gate: an email is accepted as a prospect lead iff its subject matches the
canonical pattern OR its Reply-To is an external prospect address AND the body
contains a `mailto:<that address>` link. System/marketing mail (listing-live
confirmations, newsletters, Skylines events, auth codes) all carry a StreetEasy/
no-reply Reply-To and are rejected.

NOTE: StreetEasy may duplicate-send the same inquiry and conversation follow-ups
are not deduped here. This is a known future improvement — dedup is intentionally
deferred to the dispatch layer, not the parser.
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

SUBJECT_PATTERN = re.compile(r"^(?P<address>.+?)\s+StreetEasy Inquiry From\s+(?P<name>.+)$")

NEW_MESSAGE_SUBJECT_PATTERN = re.compile(r"^New Message From\s+(?P<name>.+)$")

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

# New Message variant — the prospect's free text follows the heading.
_NEW_MESSAGE_HEADING_PATTERN = re.compile(
    r"You\s+Have\s+a\s+New\s+Message\s*\n+(?P<body>.+?)(?=\n\s*\S+@\S+\s|\nmailto:|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# Blocked StreetEasy/Zillow domains for Reply-To gate.
_BLOCKED_REPLY_TO_DOMAINS = ("streeteasy.com", "zillow.com", "zillowrentals.com")
_BLOCKED_REPLY_TO_LOCALS = frozenset(("noreply", "no-reply", "donotreply", "do-not-reply"))


def _is_external_prospect(email: str | None) -> bool:
    """Return True iff `email` is a real external prospect address.

    Rejects StreetEasy/Zillow domain addresses and common no-reply local parts.
    Case-insensitive.
    """
    if not email or "@" not in email:
        return False
    local, domain = email.rsplit("@", 1)
    if any(domain.lower().endswith(d) for d in _BLOCKED_REPLY_TO_DOMAINS):
        return False
    return local.lower() not in _BLOCKED_REPLY_TO_LOCALS


def _has_prospect_contact_signal(body: str, prospect_email: str | None) -> bool:
    """Return True iff the body contains a `mailto:<prospect_email>` link.

    Real prospect emails always include a REPLY link with the prospect's address.
    This guards against a stray external Reply-To on a non-lead email.
    """
    if not prospect_email:
        return False
    return f"mailto:{prospect_email.lower()}" in body.lower()


def parse(message: Message) -> ParsedLead:
    """Parse a StreetEasy lead email into a `ParsedLead`."""
    subject = (message.get("Subject") or "").strip()
    canonical = SUBJECT_PATTERN.match(subject)
    prospect_email = extract_reply_to_email(message)

    # Resolve body before the gate so both branches can use it.
    # Plain text part is the simplest extraction surface for SE — the HTML
    # equivalent says the same thing but adds noise from layout tables.
    body = get_body_part(message, "text/plain") or ""
    if not body:
        # Some early SE messages carry only HTML. Fall back to the HTML part.
        html = get_body_part(message, "text/html") or ""
        if html:
            from .base import html_to_text

            body = html_to_text(html)

    is_prospect_message = _is_external_prospect(prospect_email) and _has_prospect_contact_signal(
        body, prospect_email
    )

    # Accept iff the subject is canonical OR the Reply-To/body signal both
    # confirm this is a real prospect email.
    # NOTE: StreetEasy duplicate-sends and conversation follow-ups are NOT
    # deduped at the parser layer — that's a known future improvement handled
    # at the dispatch layer.
    if not (canonical or is_prospect_message):
        raise ParserError(f"StreetEasy email is not a prospect lead (subject={subject!r})")

    # ── Name extraction ───────────────────────────────────────────────────────
    if canonical:
        raw_name: str | None = canonical.group("name").strip()
    else:
        new_msg_match = NEW_MESSAGE_SUBJECT_PATTERN.match(subject)
        # Body-contact-block fallback (new_msg_match is None): name precedes
        # the email address on the contact line "<name>  <email>  <mailto:...>".
        # Can't extract reliably without PII-laden parsing — fall back to None.
        raw_name = new_msg_match.group("name").strip() if new_msg_match else None

    first_name, last_name = split_name(raw_name)

    # ── Apartment address ─────────────────────────────────────────────────────
    # New Message leads are listing-agnostic — None is correct for those.
    apartment_address: str | None = canonical.group("address").strip() if canonical else None

    phone = _extract_phone(body)
    listing_url, listing_id = _extract_listing(body)
    message_body = _extract_message_body(body)

    return ParsedLead(
        source="StreetEasy",
        first_name=first_name,
        last_name=last_name,
        email=prospect_email,
        phone=phone,
        apartment_address=apartment_address,
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

    - Question variant: returns the inline question text (heading: "You Received
      a Question About <address>").
    - New Message variant: returns the prospect's free text (heading: "You Have
      a New Message").
    - Tour variant: returns the preference (e.g. "Renter's Preferred Tour: In Person")
      so the analytics surface always shows what the prospect did.
    - Returns None when neither pattern matches (defensive — surface the email
      structurally, don't guess).
    """
    if q := _QUESTION_HEADING_PATTERN.search(body):
        return _clean_message(q.group("body"))
    if n := _NEW_MESSAGE_HEADING_PATTERN.search(body):
        return _clean_message(n.group("body"))
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
