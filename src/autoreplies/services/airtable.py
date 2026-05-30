"""Airtable client — Phase 3 implementation.

Uses pyairtable. All tables and fields are referenced exclusively through
self.schema — never by display name.

Per PLAN.md the Airtable Inquiries record ID is the canonical join key — the
Supabase row's primary key, plus user_id / apartment_id, are all Airtable IDs.
This module is the issuer of those IDs.
"""

import logging
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

from autoreplies.parsers.base import ParsedLead
from autoreplies.services.address import normalize_address, split_address
from autoreplies.services.airtable_schema import PearTrackerSchema

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.token = token
        self.schema = schema
        self._api = Api(token, use_field_ids=True)
        # Per-instance cache of normalized apartment splits (record_id → split result).
        # Populated on first match_apartment_by_address call; never invalidated
        # (the client is short-lived per lead, so stale data is not a concern).
        self._apt_split_cache: dict[str, tuple[str, str, str] | None] = {}

    def _table(self, table_id: str) -> Any:
        return self._api.table(self.schema.base_id, table_id)

    # --- Lookups ---

    def find_monitored_user_by_leads_email(self, email: str) -> dict[str, Any] | None:
        """Look up a monitored user (Autoreply Enabled (Agent) = TRUE) by their Leads Email.

        Leads Email is the mailbox the production poller monitors — for most
        agents the same as their primary Email, for some (e.g. shared assistant
        inboxes) deliberately different. Only present on the PROD schema; calling
        this on a schema where leads_email is MISSING raises a clear error.
        """
        u = self.schema.users
        if u.leads_email == "MISSING":
            raise RuntimeError(
                "find_monitored_user_by_leads_email called on a schema where "
                "leads_email is MISSING (e.g. the TEST base). The harness should "
                "use find_monitored_user_by_autoreply_email instead."
            )
        formula = AND(
            EQ(Field(u.autoreply_enabled_agent), 1),
            EQ(Field(u.leads_email), email),
        )
        rows = self._table(u.id).all(formula=formula)
        return rows[0] if rows else None

    def find_monitored_user_by_autoreply_email(self, email: str) -> dict[str, Any] | None:
        """Look up a monitored user by their Autoreply Email (Agent) — the legacy
        per-user mailbox the harness still polls during the migration window.

        Production lookups go through find_monitored_user_by_leads_email instead;
        process_lead picks between the two via the `agent_lookup_by` parameter.
        """
        u = self.schema.users
        formula = AND(
            EQ(Field(u.autoreply_enabled_agent), 1),
            EQ(Field(u.autoreply_email_agent), email),
        )
        rows = self._table(u.id).all(formula=formula)
        return rows[0] if rows else None

    def list_monitored_leads_emails(self) -> list[str]:
        """Return distinct, non-empty Leads Email values for monitored users.

        Used by the production poller for mailbox discovery. Rows where Autoreply
        Enabled (Agent) is checked but Leads Email is blank are skipped with a
        WARNING — they are misconfigured and would cause missed leads. Raises on
        TEST schema where the field is MISSING.
        """
        u = self.schema.users
        if u.leads_email == "MISSING":
            raise RuntimeError(
                "list_monitored_leads_emails called on a schema where "
                "leads_email is MISSING (e.g. the TEST base). The harness should "
                "use list_monitored_autoreply_inboxes instead."
            )
        formula = EQ(Field(u.autoreply_enabled_agent), 1)
        rows = self._table(u.id).all(formula=formula, fields=[u.leads_email])
        emails: set[str] = set()
        for row in rows:
            email = row.get("fields", {}).get(u.leads_email)
            if email:
                emails.add(email)
            else:
                logger.warning(
                    "list_monitored_leads_emails: user row %s has Autoreply Enabled "
                    "but no Leads Email — skipped",
                    row.get("id", "<unknown>"),
                )
        return sorted(emails)

    def list_monitored_autoreply_inboxes(self) -> list[str]:
        """Return distinct, non-empty Autoreply Email (Agent) values for monitored users.

        Rows where Autoreply Enabled (Agent) is checked but the inbox field is blank
        are skipped with a WARNING — they are misconfigured and would cause missed leads.
        """
        u = self.schema.users
        formula = EQ(Field(u.autoreply_enabled_agent), 1)
        rows = self._table(u.id).all(formula=formula, fields=[u.autoreply_email_agent])
        inboxes: set[str] = set()
        for row in rows:
            inbox = row.get("fields", {}).get(u.autoreply_email_agent)
            if inbox:
                inboxes.add(inbox)
            else:
                logger.warning(
                    "list_monitored_autoreply_inboxes: user row %s has Autoreply Enabled "
                    "but no Autoreply Email (Agent) — skipped",
                    row.get("id", "<unknown>"),
                )
        return sorted(inboxes)

    def find_existing_user(
        self, *, email: str | None = None, phone: str | None = None
    ) -> dict[str, Any] | None:
        """Match an existing non-staff User by email or phone.

        Excludes Agents and Admins — staff rows are never returned as prospect matches.
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
        formula = AND(NE(Field(u.type), "Agent"), NE(Field(u.type), "Admin"), or_clause)
        rows = self._table(u.id).all(formula=formula)
        return rows[0] if rows else None

    # StreetEasy URL match ceiling on TEST (1254 apts as of 2026-05-16):
    #   13 apartments have /rental/<id> URLs (matchable via this strategy)
    #   531 apartments have /building/<slug>/<unit> URLs (unreachable: inbound emails
    #        only carry /rental/<id> URLs — confirmed against fixtures/anonymized/streeteasy/*.eml)
    #   708 apartments have no streeteasy URL at all
    # So this strategy hits at most ~1% of apartments; the address matcher
    # (match_apartment_by_address) is the primary path.
    def match_apartment_by_streeteasy_id(self, listing_id: str) -> dict[str, Any] | None:
        """StreetEasy URL-based match: find Apartments where Streeteasy URL contains the ID."""
        a = self.schema.apartments
        formula = FIND(listing_id, Field(a.streeteasy))
        rows = self._table(a.id).all(formula=formula)
        return rows[0] if rows else None

    def match_apartment_by_address(self, address: str) -> tuple[dict[str, Any], int] | None:
        """Structured match: exact house_no + fuzzy street (token_set_ratio ≥ 88) + exact unit.

        Both sides are normalized via normalize_address/split_address before comparison.
        Returns (record, street_score) on match, None on no match. The score becomes
        apartment_match_confidence on the Drafts row.

        The internal street-similarity floor of 88 is a code-level constant — not
        user-tunable. The FailSafe column captures raw parsed addresses for no-match audits.
        """
        _STREET_THRESHOLD = 88
        parsed = split_address(normalize_address(address))
        if parsed is None:
            return None
        p_house, p_street, p_unit = parsed

        a = self.schema.apartments
        rows = self._table(a.id).all(fields=[a.full_address, a.apartment])

        # Build candidate list — apartments that share exact house number and unit.
        candidates: list[tuple[dict[str, Any], str]] = []
        for r in rows:
            rec_id = r["id"]
            if rec_id not in self._apt_split_cache:
                raw = r["fields"].get(a.full_address) or ""
                self._apt_split_cache[rec_id] = split_address(normalize_address(raw))
            c = self._apt_split_cache[rec_id]
            if c and c[0] == p_house and c[2] == p_unit:
                candidates.append((r, c[1]))

        if not candidates:
            return None

        scored = [(r, int(fuzz.token_set_ratio(p_street, c_street))) for r, c_street in candidates]
        best_row, best_score = max(scored, key=lambda t: t[1])
        if best_score < _STREET_THRESHOLD:
            return None
        return best_row, best_score

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
            inq.method: "Web",
            inq.type_non_website: parsed.source,
            inq.name_form: name,
            inq.email_form: parsed.email or "",
            inq.message: parsed.message_body or "",
            inq.gmail_message_id_autoreply: gmail_message_id,
        }
        if parsed.phone:
            fields[inq.phone] = parsed.phone
        if apartment_record_id:
            fields[inq.apartment] = [apartment_record_id]
        if parsed.apartment_address:
            fields[inq.apartment_failsafe] = parsed.apartment_address
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

    def update_inquiry_autoreply_body(
        self,
        inquiry_record_id: str,
        plaintext_body: str,
        gmail_message_id: str,
    ) -> None:
        """Write the sent reply body and Gmail message-id back to the Inquiries row.

        Called by send_reply_job after a successful Gmail send. Writes both
        `reply_autoreply` (plaintext body for human review) and
        `gmail_message_id_autoreply` (message-id for Gmail cross-reference)
        in a single PATCH.
        """
        inq = self.schema.inquiries
        if inq.reply_autoreply == "MISSING":
            logger.warning(
                "update_inquiry_autoreply_body: reply_autoreply field is MISSING in schema "
                "(TEST base?); skipping body write for record %s",
                inquiry_record_id,
            )
            return
        fields: dict[str, Any] = {
            inq.reply_autoreply: plaintext_body,
            inq.gmail_message_id_autoreply: gmail_message_id,
        }
        self._table(inq.id).update(inquiry_record_id, fields)

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
        sender: str,
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
            d.inquiry: [inquiry_record_id],
            d.gmail_message_id: gmail_message_id,
            d.recipient: recipient,
            d.subject: subject,
            d.body_plaintext: body_plaintext,
            d.body_html: body_html,
            d.source: source,
            d.parser_used: parser_used,
            d.template_source: template_source,
            d.reply_route: reply_route,
            d.apartment_match_strategy: apartment_match_strategy,
            d.llm_model: llm_model,
            d.sender: sender,
            d.notes_warnings: notes_warnings,
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
