"""Tests for harness/diff.py — HarnessDiff + DiffRow + to_csv."""

from __future__ import annotations

import argparse
import csv
import io
import pathlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from autoreplies.harness.diff import (
    DiffRow,
    HarnessDiff,
    _norm_phone,
    _norm_text,
    to_csv,
)
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import PROD, TEST

# ── schema field IDs (both bases share the same Inquiries field IDs) ──────────
INQ = PROD.inquiries  # field IDs identical in both bases


# ── helpers ───────────────────────────────────────────────────────────────────

def _record(rec_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"id": rec_id, "fields": fields}


def _inq_row(
    rec_id: str,
    *,
    gmail_message_id: str,
    name_form: str = "",
    email_form: str = "",
    phone: str = "",
    message: str = "",
    apartment_record_id: str | None = None,
    user_record_id: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        INQ.gmail_message_id_autoreply: gmail_message_id,
        INQ.name_form: name_form,
        INQ.email_form: email_form,
        INQ.phone: phone,
        INQ.message: message,
    }
    if apartment_record_id:
        fields[INQ.apartment] = [apartment_record_id]
    if user_record_id:
        fields[INQ.user] = [user_record_id]
    return _record(rec_id, fields)


def _make_diff(
    *,
    prod_inq_rows: list[dict[str, Any]],
    test_inq_rows: list[dict[str, Any]],
    prod_apt_rows: list[dict[str, Any]] | None = None,
    test_apt_rows: list[dict[str, Any]] | None = None,
    prod_user_rows: list[dict[str, Any]] | None = None,
    test_user_rows: list[dict[str, Any]] | None = None,
) -> HarnessDiff:
    prod_apt_rows = prod_apt_rows or []
    test_apt_rows = test_apt_rows or []
    prod_user_rows = prod_user_rows or []
    test_user_rows = test_user_rows or []

    def _make_client(
        schema: Any,
        apt_rows: list[dict[str, Any]],
        user_rows: list[dict[str, Any]],
        inq_rows: list[dict[str, Any]],
    ) -> AirtableClient:
        client = MagicMock(spec=AirtableClient)
        client.schema = schema

        def _table(table_id: str) -> MagicMock:
            tbl = MagicMock()
            if table_id == schema.apartments.id:
                tbl.all.return_value = apt_rows
            elif table_id == schema.users.id:
                tbl.all.return_value = user_rows
            elif table_id == schema.inquiries.id:
                tbl.all.return_value = inq_rows
            else:
                tbl.all.return_value = []
            return tbl

        client._table.side_effect = _table
        return client

    prod_client = _make_client(PROD, prod_apt_rows, prod_user_rows, prod_inq_rows)
    test_client = _make_client(TEST, test_apt_rows, test_user_rows, test_inq_rows)
    return HarnessDiff(prod_client, test_client)


# ── join cases ────────────────────────────────────────────────────────────────

def test_both_present_matching() -> None:
    shared_id = "msg-001"
    prod_rows = [_inq_row("recP1", gmail_message_id=shared_id, name_form="Jane Smith")]
    test_rows = [_inq_row("recT1", gmail_message_id=shared_id, name_form="Jane Smith")]

    diff = _make_diff(prod_inq_rows=prod_rows, test_inq_rows=test_rows)
    result = diff.compute("2026-05-01")

    assert len(result) == 1
    row = result[0]
    assert row.gmail_message_id == shared_id
    assert row.in_prod is True
    assert row.in_test is True
    assert row.name_form_match == "yes"


def test_both_present_diverging_apartment() -> None:
    shared_id = "msg-002"
    prod_apt = _record("recAPT_P", {PROD.apartments.full_address: "123 Main St"})
    test_apt = _record("recAPT_T", {TEST.apartments.full_address: "456 Broadway"})

    prod_rows = [_inq_row("recP2", gmail_message_id=shared_id, apartment_record_id="recAPT_P")]
    test_rows = [_inq_row("recT2", gmail_message_id=shared_id, apartment_record_id="recAPT_T")]

    diff = _make_diff(
        prod_inq_rows=prod_rows,
        test_inq_rows=test_rows,
        prod_apt_rows=[prod_apt],
        test_apt_rows=[test_apt],
    )
    result = diff.compute("2026-05-01")

    assert len(result) == 1
    row = result[0]
    assert row.apartment_agreement == "no"
    assert row.prod_apartment_address == "123 Main St"
    assert row.test_apartment_address == "456 Broadway"


def test_both_present_diverging_user() -> None:
    shared_id = "msg-003"
    prod_user = _record("recUSER_P", {PROD.users.email: "prod@example.com"})
    test_user = _record("recUSER_T", {TEST.users.email: "test@example.com"})

    prod_rows = [_inq_row("recP3", gmail_message_id=shared_id, user_record_id="recUSER_P")]
    test_rows = [_inq_row("recT3", gmail_message_id=shared_id, user_record_id="recUSER_T")]

    diff = _make_diff(
        prod_inq_rows=prod_rows,
        test_inq_rows=test_rows,
        prod_user_rows=[prod_user],
        test_user_rows=[test_user],
    )
    result = diff.compute("2026-05-01")

    row = result[0]
    assert row.user_agreement == "no"
    assert row.prod_user_email == "prod@example.com"
    assert row.test_user_email == "test@example.com"


def test_prod_only() -> None:
    prod_rows = [_inq_row("recP4", gmail_message_id="msg-prod-only")]
    diff = _make_diff(prod_inq_rows=prod_rows, test_inq_rows=[])
    result = diff.compute("2026-05-01")

    assert len(result) == 1
    row = result[0]
    assert row.in_prod is True
    assert row.in_test is False
    assert row.notes == "prod_only"


def test_test_only() -> None:
    test_rows = [_inq_row("recT5", gmail_message_id="msg-test-only")]
    diff = _make_diff(prod_inq_rows=[], test_inq_rows=test_rows)
    result = diff.compute("2026-05-01")

    assert len(result) == 1
    row = result[0]
    assert row.in_prod is False
    assert row.in_test is True
    assert row.notes == "test_only"


# ── apartment_agreement all five outcomes ─────────────────────────────────────

def test_apartment_agreement_yes() -> None:
    shared_id = "msg-apt-yes"
    addr = "100 Water St, New York, NY 10005"
    prod_apt = _record("recAPT_PA", {PROD.apartments.full_address: addr})
    test_apt = _record("recAPT_TA", {TEST.apartments.full_address: addr})

    prod_rows = [_inq_row("recP", gmail_message_id=shared_id, apartment_record_id="recAPT_PA")]
    test_rows = [_inq_row("recT", gmail_message_id=shared_id, apartment_record_id="recAPT_TA")]

    diff = _make_diff(
        prod_inq_rows=prod_rows, test_inq_rows=test_rows,
        prod_apt_rows=[prod_apt], test_apt_rows=[test_apt],
    )
    assert diff.compute("2026-05-01")[0].apartment_agreement == "yes"


def test_apartment_agreement_no() -> None:
    shared_id = "msg-apt-no"
    prod_apt = _record("recAPT_P", {PROD.apartments.full_address: "100 A St"})
    test_apt = _record("recAPT_T", {TEST.apartments.full_address: "200 B Ave"})

    prod_rows = [_inq_row("recP", gmail_message_id=shared_id, apartment_record_id="recAPT_P")]
    test_rows = [_inq_row("recT", gmail_message_id=shared_id, apartment_record_id="recAPT_T")]

    diff = _make_diff(
        prod_inq_rows=prod_rows, test_inq_rows=test_rows,
        prod_apt_rows=[prod_apt], test_apt_rows=[test_apt],
    )
    assert diff.compute("2026-05-01")[0].apartment_agreement == "no"


def test_apartment_agreement_prod_only() -> None:
    shared_id = "msg-apt-prod"
    prod_apt = _record("recAPT_P", {PROD.apartments.full_address: "100 A St"})

    prod_rows = [_inq_row("recP", gmail_message_id=shared_id, apartment_record_id="recAPT_P")]
    test_rows = [_inq_row("recT", gmail_message_id=shared_id)]

    diff = _make_diff(
        prod_inq_rows=prod_rows, test_inq_rows=test_rows,
        prod_apt_rows=[prod_apt],
    )
    assert diff.compute("2026-05-01")[0].apartment_agreement == "prod_only"


def test_apartment_agreement_test_only() -> None:
    shared_id = "msg-apt-test"
    test_apt = _record("recAPT_T", {TEST.apartments.full_address: "200 B Ave"})

    prod_rows = [_inq_row("recP", gmail_message_id=shared_id)]
    test_rows = [_inq_row("recT", gmail_message_id=shared_id, apartment_record_id="recAPT_T")]

    diff = _make_diff(
        prod_inq_rows=prod_rows, test_inq_rows=test_rows,
        test_apt_rows=[test_apt],
    )
    assert diff.compute("2026-05-01")[0].apartment_agreement == "test_only"


def test_apartment_agreement_neither() -> None:
    shared_id = "msg-apt-neither"
    prod_rows = [_inq_row("recP", gmail_message_id=shared_id)]
    test_rows = [_inq_row("recT", gmail_message_id=shared_id)]

    diff = _make_diff(prod_inq_rows=prod_rows, test_inq_rows=test_rows)
    assert diff.compute("2026-05-01")[0].apartment_agreement == "neither"


# ── phone normalization ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "(212) 555-1234",
    "212.555.1234",
    "+1-212-555-1234",
    "2125551234",
])
def test_phone_normalization_equal(raw: str) -> None:
    assert _norm_phone(raw) == _norm_phone("2125551234")


def test_phone_normalization_different() -> None:
    assert _norm_phone("212-555-0001") != _norm_phone("212-555-0002")


def test_phone_normalization_none() -> None:
    assert _norm_phone(None) == ""
    assert _norm_phone("") == ""


# ── name normalization ────────────────────────────────────────────────────────

def test_name_normalization_whitespace_and_case() -> None:
    assert _norm_text("  Jane   Smith  ") == _norm_text("jane smith")
    assert _norm_text("JANE SMITH") == _norm_text("jane smith")


def test_name_normalization_none() -> None:
    assert _norm_text(None) == ""


# ── CSV shape ─────────────────────────────────────────────────────────────────

def test_to_csv_header_only_on_empty() -> None:
    csv_text = to_csv([])
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert rows == []
    # header must still be present
    assert "gmail_message_id" in csv_text
    assert "apartment_agreement" in csv_text


def test_to_csv_shape() -> None:
    row = DiffRow(
        gmail_message_id="msg-xyz",
        in_prod=True,
        in_test=True,
        prod_apartment_address="100 Main St",
        test_apartment_address="100 Main St",
        apartment_agreement="yes",
        prod_user_email="u@example.com",
        test_user_email="u@example.com",
        user_agreement="yes",
        name_form_match="yes",
        phone_match="neither",
        message_match="yes",
        notes="",
    )
    csv_text = to_csv([row])
    reader = csv.DictReader(io.StringIO(csv_text))
    csv_rows = list(reader)
    assert len(csv_rows) == 1
    assert csv_rows[0]["gmail_message_id"] == "msg-xyz"
    assert csv_rows[0]["apartment_agreement"] == "yes"
    assert csv_rows[0]["in_prod"] == "True"


# ── _cmd_diff integration ─────────────────────────────────────────────────────

def test_cmd_diff_writes_csv_to_file(tmp_path: pathlib.Path) -> None:
    from autoreplies.harness.runner import _cmd_diff

    out_path = tmp_path / "diff.csv"
    args = argparse.Namespace(since="2026-05-01", out=str(out_path))

    mock_prod = MagicMock()
    mock_test = MagicMock()

    with (
        patch(
            "autoreplies.harness.pipeline.build_production_airtable_client_readonly",
            return_value=mock_prod,
        ),
        patch(
            "autoreplies.harness.pipeline.build_harness_airtable_client",
            return_value=mock_test,
        ),
        patch("autoreplies.harness.diff.HarnessDiff") as MockDiff,
    ):
        mock_diff_instance = MockDiff.return_value
        mock_diff_instance.compute.return_value = [
            DiffRow(
                gmail_message_id="msg-A",
                in_prod=True,
                in_test=True,
                prod_apartment_address="",
                test_apartment_address="",
                apartment_agreement="neither",
                prod_user_email="",
                test_user_email="",
                user_agreement="neither",
                name_form_match="yes",
                phone_match="neither",
                message_match="neither",
                notes="",
            )
        ]
        result = _cmd_diff(args)

    assert result == 0
    written = out_path.read_text(encoding="utf-8")
    assert "gmail_message_id" in written
    assert "msg-A" in written
