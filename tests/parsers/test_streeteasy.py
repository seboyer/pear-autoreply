"""Tests for parsers/streeteasy.py — fixture-driven extraction asserts.

Each entry in `STREETEASY_CASES` names an `.eml` fixture under
`fixtures/anonymized/streeteasy/` plus the expected `ParsedLead` field values.
Per-fixture parametrization makes diff output show which lead regressed.
"""

from __future__ import annotations

from typing import Any

import pytest

from autoreplies.parsers import parse as dispatch_parse
from autoreplies.parsers.base import ParserError
from autoreplies.parsers.streeteasy import parse

from .conftest import load_fixture

STREETEASY_CASES: list[dict[str, Any]] = [
    {
        "path": "streeteasy/tour__267-clifton-place-1a__0.eml",
        "first_name": "Katie",
        "last_name": "Shepherd",
        "email": "katie.ly.shepherd@gmail.com",
        "phone": "3362665268",
        "apartment_address": "267 Clifton Place #1A",
        "listing_id": "5018624",
        "listing_url_contains": "streeteasy.com/rental/5018624",
        "message_body_contains": "Renter's Preferred Tour",
    },
    {
        "path": "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml",
        "first_name": "Grace",
        "last_name": "Xu",
        "email": "xugrace10@gmail.com",
        "phone": "9498720059",
        "apartment_address": "65 Saint Mark's Avenue #2B",
        "listing_id": "5021171",
        "listing_url_contains": "streeteasy.com/rental/5021171",
        "message_body_contains": "Renter's Preferred Tour",
    },
    {
        "path": "streeteasy/tour__267-clifton-place-1a__22.eml",
        "first_name": "Phoebe",
        "last_name": "Davis",
        "email": "phoebeelizabeth00@gmail.com",
        "phone": "9737384978",
        "apartment_address": "267 Clifton Place #1A",
        "listing_id": "5018624",
        "listing_url_contains": "streeteasy.com/rental/5018624",
        "message_body_contains": "Renter's Preferred Tour",
    },
    {
        "path": "streeteasy/tour__267-clifton-place-1a__34.eml",
        "first_name": "Emily",
        "last_name": "Kleypas",
        "email": "emily.kleypas@gmail.com",
        "phone": "2549131395",
        "apartment_address": "267 Clifton Place #1A",
        "listing_id": "5018624",
        "listing_url_contains": "streeteasy.com/rental/5018624",
        "message_body_contains": "Renter's Preferred Tour",
    },
    {
        # Prospect entered an email address as their display name — first_name
        # falls back to None per the single-token-is-ambiguous split rule.
        "path": "streeteasy/tour__65-saint-mark-s-avenue-2b__64.eml",
        "first_name": None,
        "last_name": "avollavanh@gmail.com",
        "email": "avollavanh@gmail.com",
        "phone": "9082657975",
        "apartment_address": "65 Saint Mark's Avenue #2B",
        "listing_id": "5021171",
        "listing_url_contains": "streeteasy.com/rental/5021171",
        "message_body_contains": "Renter's Preferred Tour",
    },
    {
        "path": "streeteasy/tour__1186-bushwick-avenue-3f__89.eml",
        "first_name": "Mike",
        "last_name": "Giovannone",
        "email": "mikegiovannone@gmail.com",
        "phone": "3475522717",
        "apartment_address": "1186 Bushwick Avenue #3F",
        "listing_id": "5013752",
        "listing_url_contains": "streeteasy.com/rental/5013752",
        "message_body_contains": "Renter's Preferred Tour",
    },
    {
        "path": "streeteasy/tour__1186-bushwick-avenue-3f__99.eml",
        "first_name": "Dan",
        "last_name": "Docimo",
        "email": "ddocimo@gmail.com",
        "phone": "4129963528",
        "apartment_address": "1186 Bushwick Avenue #3F",
        "listing_id": "5013752",
        "listing_url_contains": "streeteasy.com/rental/5013752",
        "message_body_contains": "Renter's Preferred Tour",
    },
    {
        "path": "streeteasy/question__2266-pacific-street-1b__6.eml",
        "first_name": "Kyra",
        "last_name": "G. Cobb",
        "email": "kyracobb@gmail.com",
        "phone": "7734696371",
        "apartment_address": "2266 Pacific Street #1B",
        "listing_id": "5021270",
        "listing_url_contains": "streeteasy.com/rental/5021270",
        "message_body_contains": "garbage cans",
    },
    {
        # "Ms. Gray" — title prefix stripped, single remaining token → first_name=None.
        "path": "streeteasy/question__108-05-101st-avenue-3__249.eml",
        "first_name": None,
        "last_name": "Gray",
        "email": "ashantig123@gmail.com",
        "phone": "2123901698",
        "apartment_address": "108-05 101st Avenue #3",
        "listing_id": "5017092",
        "listing_url_contains": "streeteasy.com/rental/5017092",
        "message_body_contains": "interested",
    },
    {
        "path": "streeteasy/question__65-saint-mark-s-avenue-2b__440.eml",
        "first_name": "Kevin",
        "last_name": "Goshay",
        "email": "kevingoshay@gmail.com",
        "phone": "6305121407",
        "apartment_address": "65 Saint Mark's Avenue #2B",
        "listing_id": "5021171",
        "listing_url_contains": "streeteasy.com/rental/5021171",
        "message_body_contains": "interested",
    },
]


@pytest.mark.parametrize("case", STREETEASY_CASES, ids=lambda c: c["path"].split("/")[-1])
def test_streeteasy_fixture(case: dict[str, Any]) -> None:
    msg = load_fixture(case["path"])
    lead = parse(msg)

    assert lead.source == "StreetEasy"
    assert lead.parser_used == "streeteasy"
    assert lead.first_name == case["first_name"]
    assert lead.last_name == case["last_name"]
    assert lead.email == case["email"]
    assert lead.phone == case["phone"]
    assert lead.apartment_address == case["apartment_address"]
    assert lead.listing_id == case["listing_id"]
    assert lead.listing_url is not None
    assert case["listing_url_contains"] in lead.listing_url
    assert lead.message_body is not None
    assert case["message_body_contains"].lower() in lead.message_body.lower()


def test_streeteasy_dispatcher_routes_correctly() -> None:
    """Verify the top-level `parse()` dispatcher reaches the SE parser."""
    msg = load_fixture(STREETEASY_CASES[0]["path"])
    lead = dispatch_parse(msg)
    assert lead.source == "StreetEasy"


# ── error paths ───────────────────────────────────────────────────────────────


def test_streeteasy_subject_mismatch_raises_parser_error() -> None:
    """Non-lead emails from the SE sender (newsletters, magic codes, listing-live
    confirmations) must raise ParserError so callers can drop or LLM-fallback."""
    import email
    from email import policy

    raw = (
        b"From: StreetEasy <noreply@email.streeteasy.com>\r\n"
        b"To: agent@pearnyc.com\r\n"
        b"Subject: Congrats, 551 Ridgewood Avenue, #9 Is Live\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Well Done!\r\n"
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    with pytest.raises(ParserError, match="StreetEasy subject"):
        parse(msg)


def test_streeteasy_empty_body_does_not_crash() -> None:
    """A subject-matching email with no body still returns a ParsedLead with the
    fields available from the headers; body-derived fields fall to None."""
    import email
    from email import policy

    raw = (
        b"From: StreetEasy <noreply@email.streeteasy.com>\r\n"
        b"Reply-To: prospect@example.com\r\n"
        b"To: agent@pearnyc.com\r\n"
        b"Subject: 123 Main St #4B StreetEasy Inquiry From Jane Doe\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    lead = parse(msg)

    assert lead.first_name == "Jane"
    assert lead.last_name == "Doe"
    assert lead.apartment_address == "123 Main St #4B"
    assert lead.email == "prospect@example.com"
    # Body-derived fields fall to None — no crash.
    assert lead.phone is None
    assert lead.listing_url is None
    assert lead.listing_id is None
    assert lead.message_body is None
