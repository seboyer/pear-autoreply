"""Zillow lead-email parser.

Empirically (n≈10 spanning Aug 2024 → Apr 2026) the current Zillow Group
Rentals format is HTML-only — there is no `text/plain` MIME part, so plain-text
extraction returns nothing. The parser flattens the HTML alternative to text
and pulls fields from that.

Body structure (after flattening):

    Zillow Rentals Inquiry
    New Contact
    <First Last> says: "<prospect free text>"
    Income and time frame
    Credit score …
    Yearly income …
    Lease length …
    Pets …
    <email>
    <phone>
    Your listing
    FOR RENT
    …
    <address>, <borough>, NY
    …

PLAN.md § 3 previously claimed Zillow had no first_name or phone — that was an
artifact of attempting plaintext extraction on an HTML-only body. The current
empirical reality (per the H4c fixture survey) is that both are reliably
present. Downstream code still treats them as nullable per CLAUDE.md.

Listing URLs are `zillow.com/r/<opaque>` tracking redirects (not listing IDs).
We capture the first one as `listing_url`; `listing_id` is always None for
Zillow — see PLAN.md § "Empirical findings".
"""

import re
from email.message import Message

from .base import (
    ParsedLead,
    ParserError,
    extract_reply_to_email,
    get_body_part,
    html_to_text,
    split_name,
)

SUBJECT_PATTERN = re.compile(r"^New Zillow Group Rentals Contact:\s*(?P<address>.+)$")

LISTING_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?zillow\.com/r/[A-Za-z0-9]+",
    re.IGNORECASE,
)

_NEW_CONTACT_PATTERN = re.compile(
    r"New Contact\s+(?P<name>[A-Za-z][A-Za-z\.\-' ]+?)\s+says:",
    re.IGNORECASE,
)

# The prospect's free text follows `says:` in curly or straight quotes.
_SAYS_MESSAGE_PATTERN = re.compile(
    r"says:\s*[“\"](?P<msg>[^”\"]+)[”\"]",
    re.IGNORECASE,
)

# Phone format: `703.868.3162` / `(703) 868-3162` / `703-868-3162` / `703 868 3162`.
# The flattened body has digits-then-dots most commonly.
_PHONE_PATTERN = re.compile(
    r"\(?(?P<area>\d{3})\)?[\s\.\-]+(?P<prefix>\d{3})[\s\.\-]+(?P<line>\d{4})"
)


def parse(message: Message) -> ParsedLead:
    """Parse a Zillow lead email into a `ParsedLead`."""
    subject = (message.get("Subject") or "").strip()
    subject_match = SUBJECT_PATTERN.match(subject)
    if not subject_match:
        raise ParserError(
            f"Zillow subject does not match expected pattern: {subject!r}"
        )
    raw_address = subject_match.group("address").strip()

    html = get_body_part(message, "text/html")
    if not html:
        # Defensive: a future Zillow format change might re-introduce a plain
        # part. Try that before giving up.
        html = get_body_part(message, "text/plain") or ""
    body_text = html_to_text(html) if html else ""

    first_name, last_name = _extract_name(body_text)
    phone = _extract_phone(body_text, prospect_email=extract_reply_to_email(message))
    listing_url = _extract_listing_url(html or body_text)
    message_body = _extract_message_body(body_text)

    return ParsedLead(
        source="Zillow",
        first_name=first_name,
        last_name=last_name,
        email=extract_reply_to_email(message),
        phone=phone,
        apartment_address=raw_address,
        listing_url=listing_url,
        listing_id=None,  # Zillow URLs are opaque redirects, not listing IDs.
        message_body=message_body,
        parser_used="zillow",
    )


def _extract_name(body_text: str) -> tuple[str | None, str | None]:
    match = _NEW_CONTACT_PATTERN.search(body_text)
    if not match:
        return None, None
    raw = match.group("name").strip()
    # Strip the literal "says" suffix in case the lazy match overran.
    raw = re.sub(r"\s+says$", "", raw, flags=re.IGNORECASE)
    return split_name(raw)


def _extract_phone(body_text: str, *, prospect_email: str | None) -> str | None:
    """Find the prospect's phone number, skipping Zillow's office number.

    Zillow's footer contains "© 2006-2026" and a Seattle street address but no
    phone in the corporate block — empirically the first phone-shaped match in
    the body is the prospect's. We still anchor near the prospect's email (when
    we have one) for resilience against format drift.
    """
    candidates = [m for m in _PHONE_PATTERN.finditer(body_text)]
    if not candidates:
        return None

    # Anchor: if the prospect's email appears in the body, prefer the phone
    # immediately after it. Zillow sticks them next to each other.
    if prospect_email:
        idx = body_text.find(prospect_email)
        if idx >= 0:
            for m in candidates:
                if m.start() > idx:
                    return _digits(m)

    return _digits(candidates[0])


def _digits(m: re.Match[str]) -> str:
    return m.group("area") + m.group("prefix") + m.group("line")


def _extract_listing_url(source: str) -> str | None:
    match = LISTING_URL_PATTERN.search(source)
    return match.group(0) if match else None


def _extract_message_body(body_text: str) -> str | None:
    match = _SAYS_MESSAGE_PATTERN.search(body_text)
    return match.group("msg").strip() if match else None
