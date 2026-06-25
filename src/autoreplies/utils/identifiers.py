"""Email and phone normalization for person-identity resolution (Phase 2)."""

from __future__ import annotations


def normalize_email(email: str | None) -> str | None:
    """Strip whitespace and lowercase an email address.

    Returns None for None or blank input.
    """
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized if normalized else None


def normalize_phone_e164(phone: str | None) -> str | None:
    """Prepend +1 to a 10-digit US/CA phone number.

    The parsers strip the country code and any formatting, yielding bare
    10-digit strings for US/CA numbers. This function re-adds the E.164 +1
    prefix. Returns None for None or non-10-digit input.

    US/CA assumption: only correct for numbers where the country code is +1.
    Parsers already discard the country code; international numbers outside
    the +1 NANP are not returned by the parsers, so they arrive as None.
    """
    if not phone:
        return None
    digits = phone.strip()
    if len(digits) != 10 or not digits.isdigit():
        return None
    return f"+1{digits}"
