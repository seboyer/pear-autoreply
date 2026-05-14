"""Airtable client — Phase 3 implementation.

Uses pyairtable. All tables and fields are referenced exclusively through
self.schema — never by display name.

Per PLAN.md the Airtable Inquiries record ID is the canonical join key — the
Supabase row's primary key, plus user_id / apartment_id, are all Airtable IDs.
This module is the issuer of those IDs.
"""

from datetime import datetime
from typing import Any, Literal

from pyairtable import Api
from pyairtable.formulas import (
    AND,
    CREATED_TIME,
    DATETIME_PARSE,
    EQ,
    FIND,
    IS_AFTER,
    NE,
    OR,
    Field,
    Formula,
)
from rapidfuzz import fuzz
from rapidfuzz import process as fuzz_process

from autoreplies.parsers.base import ParsedLead
from autoreplies.services.airtable_schema import PearTrackerSchema


def created_after(since_iso: str) -> Formula:
    """Build IS_AFTER(CREATED_TIME(), DATETIME_PARSE('<since_iso>')).

    since_iso must be a valid ISO-8601 string — callers are responsible for
    pre-validating via datetime.fromisoformat so the value is already sanitized.
    """
    return IS_AFTER(CREATED_TIME(), DATETIME_PARSE(since_iso))


class AirtableClient:
    def __init__(
        self,
        token: str,
        schema: PearTrackerSchema,
        address_match_threshold: int = 92,
    ) -> None:
        self.token = token
        self.schema = schema
        self.address_match_threshold = address_match_threshold
        self._api = Api(token)

    def _table(self, table_id: str) -> Any:
        return self._api.table(self.schema.base_id, table_id)

    # --- Lookups ---

    def find_agent_by_primary_email(self, email: str) -> dict[str, Any] | None:
        """Look up an agent (Users.Type=Agent) by their primary Email field.

        Email here is the recipient mailbox the lead landed at — e.g.
        firstname@pearnyc.com.
        """
        u = self.schema.users
        formula = AND(EQ(Field(u.type), "Agent"), EQ(Field(u.email), email))
        rows = self._table(u.id).all(formula=formula)
        return rows[0] if rows else None

    def list_agent_emails(self) -> list[str]:
        """Return all distinct, non-empty Users.Email where Type=Agent, sorted."""
        u = self.schema.users
        formula = EQ(Field(u.type), "Agent")
        rows = self._table(u.id).all(formula=formula, fields=[u.email])
        emails: set[str] = set()
        for row in rows:
            email = row.get("fields", {}).get(u.email)
            if email:
                emails.add(email)
        return sorted(emails)

    def find_existing_user(
        self, *, email: str | None = None, phone: str | None = None
    ) -> dict[str, Any] | None:
        """Match an existing non-agent User by email or phone.

        Per PLAN.md we only *match* — never create — users from leads.
        Returns None on no match.
        """
        if not email and not phone:
            return None
        u = self.schema.users
        or_parts: list[Formula] = []
        if email:
            or_parts.append(EQ(Field(u.email), email))
        if phone:
            or_parts.append(EQ(Field(u.phone), phone))
        or_clause: Formula = OR(*or_parts) if len(or_parts) > 1 else or_parts[0]
        formula = AND(NE(Field(u.type), "Agent"), or_clause)
        rows = self._table(u.id).all(formula=formula)
        return rows[0] if rows else None

    def match_apartment_by_streeteasy_id(self, listing_id: str) -> dict[str, Any] | None:
        """StreetEasy URL-based match: find Apartments where Streeteasy URL contains the ID."""
        a = self.schema.apartments
        formula = FIND(listing_id, Field(a.streeteasy))
        rows = self._table(a.id).all(formula=formula)
        return rows[0] if rows else None

    def match_apartment_by_address(
        self, normalized_address: str, threshold: int | None = None
    ) -> dict[str, Any] | None:
        """Address-fuzzy match against Apartments.Full Address (rapidfuzz WRatio).

        `threshold` overrides the instance default (set from
        `settings.apartment_fuzzy_match_threshold`).
        """
        effective_threshold = threshold if threshold is not None else self.address_match_threshold
        a = self.schema.apartments
        rows = self._table(a.id).all(fields=[a.full_address, a.apartment])
        if not rows:
            return None
        choices = {r["id"]: (r["fields"].get(a.full_address) or "") for r in rows}
        result = fuzz_process.extractOne(
            normalized_address,
            choices,
            scorer=fuzz.WRatio,
            score_cutoff=effective_threshold,
        )
        if result is None:
            return None
        _match_str, _score, record_id = result
        return next(r for r in rows if r["id"] == record_id)

    def find_inquiry_by_gmail_message_id(self, message_id: str) -> dict[str, Any] | None:
        """Durable backstop for idempotency: look up an Inquiry by Gmail Message ID (Autoreply)."""
        inq = self.schema.inquiries
        formula = EQ(Field(inq.gmail_message_id_autoreply), message_id)
        rows = self._table(inq.id).all(formula=formula)
        return rows[0] if rows else None

    # --- Writes ---

    def create_inquiry(
        self,
        *,
        gmail_message_id: str,
        parsed: ParsedLead,
        apartment_record_id: str | None,
        user_record_id: str | None,
    ) -> str:
        """Create an Inquiries row and return the new Airtable record ID.

        The returned ID becomes the Supabase primary key for the same lead.
        Agent is NOT written — it is a lookup through the linked Apartment.

        We do not persist the *sent reply* Gmail message-id here — there is no
        curated Inquiries field for it (intentional). It's tracked in
        JobState.reply_sent_message_id for in-pipeline use; if we later need
        durable storage, add a field to CURATED in
        scripts/generate_airtable_schema.py and plumb it through.
        """
        inq = self.schema.inquiries
        name = " ".join(part for part in (parsed.first_name, parsed.last_name) if part)
        fields: dict[str, Any] = {
            inq.method:                     "Web",
            inq.type_non_website:           parsed.source,
            inq.name_form:                  name,
            inq.email_form:                 parsed.email or "",
            inq.message:                    parsed.message_body or "",
            inq.gmail_message_id_autoreply: gmail_message_id,
        }
        if parsed.phone:
            fields[inq.phone] = parsed.phone
        if apartment_record_id:
            fields[inq.apartment] = [apartment_record_id]
        if user_record_id:
            fields[inq.user] = [user_record_id]
        # Agent is a lookup through Apartment — never written directly.
        record = self._table(inq.id).create(fields)
        return record["id"]

    def find_or_create_inquiry(
        self,
        *,
        gmail_message_id: str,
        parsed: ParsedLead,
        apartment_record_id: str | None,
        user_record_id: str | None,
    ) -> str:
        """Return the existing Inquiry record ID for this message, or create one.

        Used by the harness pipeline for idempotent re-runs of the same message.
        Production uses Redis-backed dedup in process_lead instead.
        """
        existing = self.find_inquiry_by_gmail_message_id(gmail_message_id)
        if existing:
            return existing["id"]
        return self.create_inquiry(
            gmail_message_id=gmail_message_id,
            parsed=parsed,
            apartment_record_id=apartment_record_id,
            user_record_id=user_record_id,
        )

    def create_draft(
        self,
        *,
        inquiry_record_id: str,
        gmail_message_id: str,
        recipient: str,
        subject: str,
        body_plaintext: str,
        body_html: str,
        source: Literal["StreetEasy", "Zillow"],
        parser_used: Literal["regex", "llm_fallback"],
        template_source: Literal["agent", "pear_default"],
        reply_route: Literal["thread", "direct", "skipped"],
        apartment_match_strategy: Literal["streeteasy_id", "address", "none"],
        llm_model: str,
        notes_warnings: str = "",
        skipped_reason: str | None = None,
        apartment_match_confidence: int | None = None,
        llm_latency_ms: int | None = None,
        would_send_at: datetime | None = None,
    ) -> str:
        """Write a Drafts row to the test base and return the new record ID.

        The parser_used taxonomy mapping (streeteasy/zillow → regex) is the
        caller's responsibility (DraftSend in harness/). This method writes
        whatever Literal value is passed verbatim.
        """
        d = self.schema.drafts
        fields: dict[str, Any] = {
            d.inquiry:                   [inquiry_record_id],
            d.gmail_message_id:          gmail_message_id,
            d.recipient:                 recipient,
            d.subject:                   subject,
            d.body_plaintext:            body_plaintext,
            d.body_html:                 body_html,
            d.source:                    source,
            d.parser_used:               parser_used,
            d.template_source:           template_source,
            d.reply_route:               reply_route,
            d.apartment_match_strategy:  apartment_match_strategy,
            d.llm_model:                 llm_model,
            d.notes_warnings:            notes_warnings,
        }
        if skipped_reason is not None:
            fields[d.skipped_reason] = skipped_reason
        if apartment_match_confidence is not None:
            fields[d.apartment_match_confidence] = apartment_match_confidence
        if llm_latency_ms is not None:
            fields[d.llm_latency_ms] = llm_latency_ms
        if would_send_at is not None:
            fields[d.would_send_at] = would_send_at.isoformat()
        record = self._table(d.id).create(fields)
        return record["id"]
