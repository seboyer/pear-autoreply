"""Harness diff report — cross-base comparison of Inquiries in PROD vs TEST."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Literal

from pyairtable.formulas import AND, NE, Field

from autoreplies.services.airtable import AirtableClient, created_after

Agreement = Literal["yes", "no", "prod_only", "test_only", "neither"]


def _norm_text(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(s.casefold().split())


def _norm_phone(s: str | None) -> str:
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    # Strip US country code prefix so +1-212-555-1234 == 2125551234
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _norm_email(s: str | None) -> str:
    if not s:
        return ""
    return s.strip().casefold()


def _compare(
    prod_val: str | None,
    test_val: str | None,
    norm_fn: Any = None,
) -> Agreement:
    p = norm_fn(prod_val) if norm_fn else (prod_val or "")
    t = norm_fn(test_val) if norm_fn else (test_val or "")
    if not p and not t:
        return "neither"
    if p and not t:
        return "prod_only"
    if not p and t:
        return "test_only"
    return "yes" if p == t else "no"


@dataclass
class DiffRow:
    gmail_message_id: str
    in_prod: bool
    in_test: bool
    prod_apartment_address: str
    test_apartment_address: str
    apartment_agreement: Agreement
    prod_user_email: str
    test_user_email: str
    user_agreement: Agreement
    name_form_match: Agreement
    phone_match: Agreement
    message_match: Agreement
    notes: str


_CSV_FIELDS = [
    "gmail_message_id",
    "in_prod",
    "in_test",
    "prod_apartment_address",
    "test_apartment_address",
    "apartment_agreement",
    "prod_user_email",
    "test_user_email",
    "user_agreement",
    "name_form_match",
    "phone_match",
    "message_match",
    "notes",
]


def to_csv(rows: list[DiffRow]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "gmail_message_id": row.gmail_message_id,
            "in_prod": row.in_prod,
            "in_test": row.in_test,
            "prod_apartment_address": row.prod_apartment_address,
            "test_apartment_address": row.test_apartment_address,
            "apartment_agreement": row.apartment_agreement,
            "prod_user_email": row.prod_user_email,
            "test_user_email": row.test_user_email,
            "user_agreement": row.user_agreement,
            "name_form_match": row.name_form_match,
            "phone_match": row.phone_match,
            "message_match": row.message_match,
            "notes": row.notes,
        })
    return buf.getvalue()


class HarnessDiff:
    """Cross-base diff of Inquiries between PROD and TEST Airtable bases."""

    def __init__(
        self,
        prod_airtable: AirtableClient,
        test_airtable: AirtableClient,
    ) -> None:
        self._prod = prod_airtable
        self._test = test_airtable

        # Pre-load lookup dicts on construction
        self._prod_apartment_address = self._load_addresses(prod_airtable)
        self._test_apartment_address = self._load_addresses(test_airtable)
        self._prod_user_email = self._load_user_emails(prod_airtable)
        self._test_user_email = self._load_user_emails(test_airtable)

    def _load_addresses(self, airtable: AirtableClient) -> dict[str, str]:
        a = airtable.schema.apartments
        rows = airtable._table(a.id).all(fields=[a.full_address])
        return {r["id"]: r.get("fields", {}).get(a.full_address) or "" for r in rows}

    def _load_user_emails(self, airtable: AirtableClient) -> dict[str, str]:
        u = airtable.schema.users
        rows = airtable._table(u.id).all(fields=[u.email])
        return {r["id"]: r.get("fields", {}).get(u.email) or "" for r in rows}

    def _resolve_apartment(self, row: dict[str, Any], address_map: dict[str, str]) -> str:
        inq_schema = self._prod.schema.inquiries  # field IDs are identical across bases
        linked = row.get("fields", {}).get(inq_schema.apartment)
        if not linked:
            return ""
        record_id = linked[0] if isinstance(linked, list) else linked
        return address_map.get(record_id, "")

    def _resolve_user(self, row: dict[str, Any], email_map: dict[str, str]) -> str:
        inq_schema = self._prod.schema.inquiries
        linked = row.get("fields", {}).get(inq_schema.user)
        if not linked:
            return ""
        record_id = linked[0] if isinstance(linked, list) else linked
        return email_map.get(record_id, "")

    def compute(self, since_iso: str) -> list[DiffRow]:
        from datetime import datetime
        datetime.fromisoformat(since_iso)  # validate

        prod_inq = self._prod.schema.inquiries
        test_inq = self._test.schema.inquiries

        prod_formula = AND(
            created_after(since_iso),
            NE(Field(prod_inq.gmail_message_id_autoreply), ""),
        )
        test_formula = created_after(since_iso)

        prod_rows = self._prod._table(prod_inq.id).all(formula=prod_formula)
        test_rows = self._test._table(test_inq.id).all(formula=test_formula)

        # Index by Gmail Message ID (Autoreply)
        prod_by_id: dict[str, dict[str, Any]] = {}
        for row in prod_rows:
            mid = row.get("fields", {}).get(prod_inq.gmail_message_id_autoreply)
            if mid:
                prod_by_id[mid] = row

        test_by_id: dict[str, dict[str, Any]] = {}
        for row in test_rows:
            mid = row.get("fields", {}).get(test_inq.gmail_message_id_autoreply)
            if mid:
                test_by_id[mid] = row

        all_ids = sorted(set(prod_by_id) | set(test_by_id))

        result: list[DiffRow] = []
        for mid in all_ids:
            prod_row = prod_by_id.get(mid)
            test_row = test_by_id.get(mid)

            in_prod = prod_row is not None
            in_test = test_row is not None

            prod_apt = self._resolve_apartment(prod_row, self._prod_apartment_address) if prod_row else ""
            test_apt = self._resolve_apartment(test_row, self._test_apartment_address) if test_row else ""

            prod_email = self._resolve_user(prod_row, self._prod_user_email) if prod_row else ""
            test_email = self._resolve_user(test_row, self._test_user_email) if test_row else ""

            def _field(row: dict[str, Any] | None, field_id: str) -> str:
                if row is None:
                    return ""
                return row.get("fields", {}).get(field_id) or ""

            prod_name = _field(prod_row, prod_inq.name_form)
            test_name = _field(test_row, test_inq.name_form)
            prod_phone = _field(prod_row, prod_inq.phone)
            test_phone = _field(test_row, test_inq.phone)
            prod_msg = _field(prod_row, prod_inq.message)
            test_msg = _field(test_row, test_inq.message)

            notes = ""
            if not in_prod:
                notes = "test_only"
            elif not in_test:
                notes = "prod_only"

            result.append(DiffRow(
                gmail_message_id=mid,
                in_prod=in_prod,
                in_test=in_test,
                prod_apartment_address=prod_apt,
                test_apartment_address=test_apt,
                apartment_agreement=_compare(prod_apt, test_apt, _norm_text),
                prod_user_email=prod_email,
                test_user_email=test_email,
                user_agreement=_compare(prod_email, test_email, _norm_email),
                name_form_match=_compare(prod_name, test_name, _norm_text),
                phone_match=_compare(prod_phone, test_phone, _norm_phone),
                message_match=_compare(prod_msg, test_msg, _norm_text),
                notes=notes,
            ))

        return result
