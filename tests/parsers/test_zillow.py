"""Tests for parsers/zillow.py — fixture-driven extraction asserts.

Each entry in `ZILLOW_CASES` names an `.eml` fixture under
`fixtures/anonymized/zillow/` plus the expected `ParsedLead` field values.
"""

from __future__ import annotations

from typing import Any

import pytest

from autoreplies.parsers import parse as dispatch_parse
from autoreplies.parsers.base import ParserError
from autoreplies.parsers.zillow import parse

from .conftest import load_fixture

ZILLOW_CASES: list[dict[str, Any]] = [
    {
        "path": "zillow/lead__170-prospect-pl-3b__1.eml",
        "first_name": "Eric",
        "last_name": "Brown",
        "email": "ecbrown2@gmail.com",
        "phone": "7038683162",
        "apartment_address": "170 Prospect Pl #3B",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "interested in your property",
    },
    {
        "path": "zillow/lead__138-montague-st-11__3788.eml",
        "first_name": "marcos",
        "last_name": "Arellano",
        "email": "lopezmarcos87@gmail.com",
        "phone": "3477561199",
        "apartment_address": "138 Montague St #11",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "schedule a viewing",
    },
    {
        "path": "zillow/lead__68-19-borden-ave-108__8042.eml",
        "first_name": "Parvin",
        "last_name": "Akter",
        "email": "poly5400@gmail.com",
        "phone": "5168005557",
        "apartment_address": "68-19 Borden Ave #108",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "interested in your property",
    },
    {
        "path": "zillow/lead__2027-stillwell-ave-3r__13329.eml",
        "first_name": "Jessica",
        "last_name": "Castro",
        "email": "cjandsteven0914@gmail.com",
        "phone": "3475752506",
        "apartment_address": "2027 Stillwell Ave #3R",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "schedule a tour",
    },
    {
        "path": "zillow/lead__1404-e-93rd-st-2f__17596.eml",
        "first_name": "Ericka",
        "last_name": "German",
        "email": "egerman4180@gmail.com",
        "phone": "9292489929",
        "apartment_address": "1404 E 93rd St #2F",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "interested in your property",
    },
    {
        "path": "zillow/lead__1431-lincoln-pl-4d__23190.eml",
        "first_name": "melanie",
        "last_name": "ortiz",
        "email": "melly.ortiz1106@gmail.com",
        "phone": "5167890006",
        "apartment_address": "1431 Lincoln Pl #4D",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "schedule a viewing",
    },
    {
        "path": "zillow/lead__61-12-woodbine-st-3c__28079.eml",
        "first_name": "Alex",
        "last_name": "cabral",
        "email": "alex-cabral26@hotmail.com",
        "phone": "9293236500",
        "apartment_address": "61-12 Woodbine St #3C",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "interested in your property",
    },
    {
        "path": "zillow/lead__59-15-woodbine-st-2r__31409.eml",
        "first_name": "Samira",
        "last_name": "Esmailzada",
        "email": "samira.esmailzada@gmail.com",
        "phone": "2018389679",
        "apartment_address": "59-15 Woodbine St #2R",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "schedule a viewing",
    },
    {
        "path": "zillow/lead__1926-madison-st-2l__35098.eml",
        "first_name": "Molly",
        "last_name": "Olson",
        "email": "mollyolson@icloud.com",
        "phone": "3473023488",
        "apartment_address": "1926 Madison St #2L",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "schedule a viewing",
    },
    {
        "path": "zillow/lead__112-30-rockaway-beach-blvd-2a__38642.eml",
        "first_name": "carlos",
        "last_name": "franceschini",
        "email": "losobandz02@icloud.com",
        "phone": "5165226755",
        "apartment_address": "112-30 Rockaway Beach Blvd #2A",
        "listing_url_contains": "zillow.com/r/",
        "message_body_contains": "schedule a viewing",
    },
]


@pytest.mark.parametrize("case", ZILLOW_CASES, ids=lambda c: c["path"].split("/")[-1])
def test_zillow_fixture(case: dict[str, Any]) -> None:
    msg = load_fixture(case["path"])
    lead = parse(msg)

    assert lead.source == "Zillow"
    assert lead.parser_used == "zillow"
    assert lead.first_name == case["first_name"]
    assert lead.last_name == case["last_name"]
    assert lead.email == case["email"]
    assert lead.phone == case["phone"]
    assert lead.apartment_address == case["apartment_address"]
    # Zillow URLs are opaque tracking redirects — listing_id is always None.
    assert lead.listing_id is None
    assert lead.listing_url is not None
    assert case["listing_url_contains"] in lead.listing_url
    assert lead.message_body is not None
    assert case["message_body_contains"].lower() in lead.message_body.lower()


def test_zillow_dispatcher_routes_correctly() -> None:
    msg = load_fixture(ZILLOW_CASES[0]["path"])
    lead = dispatch_parse(msg)
    assert lead.source == "Zillow"


def test_zillow_subject_mismatch_raises_parser_error() -> None:
    import email
    from email import policy

    raw = (
        b"From: Zillow Group Rentals <rentalclientservices@zillowrentals.com>\r\n"
        b"To: agent@pearnyc.com\r\n"
        b"Subject: Your listing was updated\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body>noise</body></html>\r\n"
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    with pytest.raises(ParserError, match="Zillow subject"):
        parse(msg)


def test_zillow_all_fixtures_have_listing_id_none() -> None:
    """Spot-check the documented invariant: Zillow URLs are opaque redirects."""
    for case in ZILLOW_CASES:
        msg = load_fixture(case["path"])
        lead = parse(msg)
        assert lead.listing_id is None, f"listing_id should be None for Zillow ({case['path']})"
