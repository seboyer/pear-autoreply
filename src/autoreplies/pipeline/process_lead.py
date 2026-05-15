"""End-to-end worker pipeline.

Mirrors the pseudocode in PLAN.md § 2. Two-phase idempotency:
- Pre-Airtable: dedup by Gmail message-id via Redis state.
- Post-Airtable: state.airtable_record_id drives downstream retries so we never
  create orphan rows on partial failure.

Phase 0 sketches the structure with explicit phase markers. Phases 1-4 fill in
the bodies one section at a time.

Side effects (send, Slack, Supabase) are injected via PipelineStrategies so the
harness can swap in DraftSend/Noop* without forking the pipeline. Production
passes no strategies arg; the default builds the Live* bundle.

Services (gmail, airtable, llm) are passed explicitly by the harness factory.
Production wiring (Phase 1) will resolve them from settings/deps instead.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from autoreplies.parsers import base as parsers_base
from autoreplies.pipeline.reply_route import (
    ReplyDestination,
    resolve_reply_destination,
    subject_for_reply,
)
from autoreplies.pipeline.strategies import PipelineStrategies, build_production_strategies
from autoreplies.services.llm import TemplateFillError
from autoreplies.services.templates import get_template_for_agent

if TYPE_CHECKING:
    from autoreplies.services.airtable import AirtableClient
    from autoreplies.services.gmail import GmailClient
    from autoreplies.services.llm import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class JobState:
    """Per-message pipeline state. Persisted in Redis keyed by Gmail message-id."""

    message_id: str
    mailbox_email: str
    airtable_record_id: str | None = None
    parsed_snapshot: dict[str, Any] | None = None     # cached parsed lead
    reply_sent_message_id: str | None = None
    supabase_done: bool = False
    slack_done: bool = False
    fully_done: bool = False
    last_error: str | None = None
    attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def process_lead(
    message_id: str,
    mailbox_email: str,
    *,
    strategies: PipelineStrategies | None = None,
    gmail: GmailClient | None = None,
    airtable: AirtableClient | None = None,
    llm: LLMClient | None = None,
) -> None:
    """Drive a single lead message through the full pipeline.

    Idempotent: safe to call N times for the same message_id. State checks at
    each phase ensure side-effects happen exactly once.

    `strategies` defaults to the production Live* bundle. The harness injects
    DraftSend/NoopSlack/NoopSupabase without changing this function's signature.

    `gmail`, `airtable`, `llm` are passed by the harness factory. When None,
    the phase bodies raise NotImplementedError until Phase 1 wires production
    service construction from settings/deps.
    """
    if strategies is None:
        strategies = build_production_strategies()

    state = _load_state(message_id, mailbox_email)
    if state.fully_done:
        logger.info("process_lead: skip (fully_done) message_id=%s", message_id)
        return

    state.attempts += 1
    _save_state(state)

    try:
        # Phase A — pre-Airtable: parse, reply, then Airtable insert.
        if not state.airtable_record_id:
            _phase_a_create_airtable(state, strategies, gmail=gmail, airtable=airtable, llm=llm)
            _save_state(state)

        # Phase B — Supabase upsert (idempotent on Airtable record ID).
        if not state.supabase_done:
            _phase_b_write_supabase(state, strategies)
            state.supabase_done = True
            _save_state(state)

        # Phase C — Slack notification.
        if not state.slack_done:
            _phase_c_post_slack(state, strategies)
            state.slack_done = True
            _save_state(state)

        state.fully_done = True
        _save_state(state)
        logger.info("process_lead: done message_id=%s record_id=%s",
                    message_id, state.airtable_record_id)

    except Exception as exc:
        state.last_error = repr(exc)
        _save_state(state)
        logger.exception("process_lead: failed message_id=%s", message_id)
        raise


# --- Phases ------------------------------------------------------------------


def _phase_a_create_airtable(
    state: JobState,
    strategies: PipelineStrategies,
    *,
    gmail: GmailClient | None,
    airtable: AirtableClient | None,
    llm: LLMClient | None,
) -> None:
    """Fetch the email, parse, generate + send the auto-reply, write Airtable.

    On success: populates state.airtable_record_id, state.parsed_snapshot,
    state.reply_sent_message_id, and state.extra for use by phases B and C.
    """
    if gmail is None or airtable is None or llm is None:
        raise NotImplementedError("Phase 1")

    # 1. Fetch raw email.
    message, thread_id = gmail.get_message(state.message_id)

    # 2. Parse the lead.
    parsed = parsers_base.parse(message)

    # 3. Match apartment.
    apartment_record, apartment_match_strategy, apartment_match_confidence = (
        _match_apartment_for_lead(airtable, parsed)
    )
    apartment_record_id = apartment_record["id"] if apartment_record else None

    # 4. Match user (never create — only match existing).
    user_record = airtable.find_existing_user(email=parsed.email, phone=parsed.phone)
    user_record_id = user_record["id"] if user_record else None

    # 5. Load agent record for the mailbox.
    agent_record = airtable.find_monitored_user_by_primary_email(state.mailbox_email)
    if agent_record is None:
        logger.warning("_phase_a: no agent record found for mailbox=%s", state.mailbox_email)

    # 6. Look up reply template (harness uses autoreply_test_template; prod uses autoreply_agent).
    schema = airtable.schema
    template_field_id = (
        schema.users.autoreply_test_template
        if schema.users.autoreply_test_template != "MISSING"
        else schema.users.autoreply_agent
    )
    template_text, template_source = get_template_for_agent(
        agent_record or {}, template_field_id=template_field_id
    )

    # 7. Fill template via LLM (falls back to literal fill; raises TemplateFillError
    #    only when a required slot has no value or default).
    slots: dict[str, Any] = {
        "first_name": parsed.first_name,
        "apartment_address": parsed.apartment_address,
    }
    fill_skipped_reason: str | None = None
    try:
        fill_result = llm.fill_template(template_text=template_text, slots=slots)
        filled_body: str = fill_result["filled_body"]
        llm_model: str = fill_result["model"]
        llm_latency_ms: int | None = int(fill_result["latency_ms"])
    except TemplateFillError as exc:
        filled_body = ""
        llm_model = llm.model
        llm_latency_ms = None
        fill_skipped_reason = f"template_fill_error: {exc}"
        logger.warning("_phase_a: template fill failed for %s: %s", state.message_id, exc)

    # 8. Resolve reply destination.
    dest = resolve_reply_destination(
        message=message, parsed=parsed, thread_id=thread_id or None
    )

    # TemplateFillError overrides route to skipped.
    if fill_skipped_reason:
        dest = ReplyDestination(
            route="skipped",
            recipient=None,
            skipped_reason=fill_skipped_reason,
            in_reply_to_message_id=dest.in_reply_to_message_id,
            thread_id=dest.thread_id,
        )

    # 9. Build subject and notes.
    incoming_subject = (message.get("Subject") or "").strip()
    reply_subject = subject_for_reply(incoming_subject)

    notes_parts: list[str] = []
    if apartment_record is None:
        notes_parts.append(f"no apartment match for {parsed.apartment_address!r}")
    if user_record is None:
        notes_parts.append("no user match")
    notes = "; ".join(notes_parts)

    # 10. Create Airtable Inquiry first so DraftSend can link the Drafts row to it.
    #     find_or_create_inquiry is idempotent — safe to retry.
    inquiry_id = airtable.find_or_create_inquiry(
        gmail_message_id=state.message_id,
        parsed=parsed,
        apartment_record_id=apartment_record_id,
        user_record_id=user_record_id,
    )

    # 11. Call send strategy. DraftSend writes the Drafts row linked to inquiry_id.
    #     LiveSend (Phase 2) sends the Gmail reply; it raises NotImplementedError until then.
    send_result = strategies.send.send_reply(
        to=dest.recipient or "",
        subject=reply_subject,
        plaintext_body=filled_body,
        html_body=filled_body,  # harness: same body for both parts; Phase 2 handles HTML+sig
        in_reply_to_message_id=dest.in_reply_to_message_id,
        thread_id=dest.thread_id,
        agent=agent_record or {},
        parsed=parsed,
        inquiry_record_id=inquiry_id,
        gmail_message_id=state.message_id,
        reply_route=dest.route,
        skipped_reason=dest.skipped_reason,
        apartment_match_strategy=apartment_match_strategy,
        apartment_match_confidence=apartment_match_confidence,
        template_source=template_source,
        llm_model=llm_model,
        llm_latency_ms=llm_latency_ms,
        notes=notes,
    )

    # 12. Update state.
    state.airtable_record_id = inquiry_id
    state.parsed_snapshot = dataclasses.asdict(parsed)
    state.reply_sent_message_id = send_result.sent_id

    agent_fields = (agent_record.get("fields") or {}) if agent_record else {}
    prospect_parts = [p for p in (parsed.first_name, parsed.last_name) if p]
    state.extra = {
        "source": parsed.source,
        "agent_name": agent_fields.get(schema.users.name, ""),
        "agent_email": state.mailbox_email,
        "prospect_name": " ".join(prospect_parts) or None,
        "prospect_email": parsed.email,
        "prospect_phone": parsed.phone,
        "apartment_address": parsed.apartment_address,
        "apartment_match_confidence": apartment_match_confidence,
        "message_excerpt": (parsed.message_body or "")[:200] if parsed.message_body else None,
        "gmail_thread_url": (
            f"https://mail.google.com/mail/u/0/#all/{dest.thread_id}"
            if dest.thread_id else ""
        ),
    }

    logger.info(
        "_phase_a: done message_id=%s inquiry_id=%s route=%s parser=%s",
        state.message_id, inquiry_id, dest.route, parsed.parser_used,
    )


def _phase_b_write_supabase(state: JobState, strategies: PipelineStrategies) -> None:
    """Upsert into Supabase using the Airtable record ID as primary key.

    NoopSupabase returns {} immediately. LiveSupabase (Phase 3) writes the full row.
    """
    strategies.supabase.upsert_inquiry(
        id=state.airtable_record_id or "",
        source=state.extra.get("source", ""),
        prospect_email=state.extra.get("prospect_email"),
        prospect_phone=state.extra.get("prospect_phone"),
        apartment_address=state.extra.get("apartment_address"),
    )


def _phase_c_post_slack(state: JobState, strategies: PipelineStrategies) -> None:
    """Post to #platform-leads with the lead summary.

    NoopSlack returns "" immediately. LiveSlack (Phase 3) formats and sends.
    """
    extra = state.extra
    strategies.slack.post_lead(
        source=extra.get("source", ""),
        agent_name=extra.get("agent_name", ""),
        agent_email=extra.get("agent_email", ""),
        prospect_name=extra.get("prospect_name"),
        prospect_email=extra.get("prospect_email"),
        prospect_phone=extra.get("prospect_phone"),
        apartment_address=extra.get("apartment_address"),
        apartment_match_confidence=extra.get("apartment_match_confidence"),
        message_excerpt=extra.get("message_excerpt"),
        airtable_record_id=state.airtable_record_id or "",
        gmail_thread_url=extra.get("gmail_thread_url", ""),
    )


# --- Helpers -----------------------------------------------------------------


def _match_apartment_for_lead(
    airtable: AirtableClient,
    parsed: parsers_base.ParsedLead,
) -> tuple[dict[str, Any] | None, Literal["streeteasy_id", "address", "none"], int | None]:
    """Return (apartment_record, match_strategy, confidence).

    Strategy priority: streeteasy_id (deterministic, confidence=100) → address
    fuzzy match (confidence=None, threshold from client settings) → none.
    """
    if parsed.listing_id:
        record = airtable.match_apartment_by_streeteasy_id(parsed.listing_id)
        if record:
            return record, "streeteasy_id", 100

    if parsed.apartment_address:
        record = airtable.match_apartment_by_address(parsed.apartment_address)
        if record:
            return record, "address", None

    return None, "none", None


# --- State store (stub) ------------------------------------------------------


def _load_state(message_id: str, mailbox_email: str) -> JobState:
    """Load JobState from Redis, or create a fresh one."""
    # TODO Phase 4: real Redis-backed implementation with TTL.
    return JobState(message_id=message_id, mailbox_email=mailbox_email)


def _save_state(state: JobState) -> None:
    """Persist JobState to Redis."""
    # TODO Phase 4: real Redis-backed implementation with TTL.
    pass
