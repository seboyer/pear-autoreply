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
from email.message import Message
from email.utils import getaddresses
from typing import TYPE_CHECKING, Any, Literal

from autoreplies.parsers import base as parsers_base
from autoreplies.pipeline.dedup import (
    DedupStore,
    DuplicateLeadSuppressed,
    NoopDedup,
    compute_fingerprint,
)
from autoreplies.pipeline.identity import NoopResolver, PersonResolver
from autoreplies.pipeline.reply_route import (
    ReplyDestination,
    resolve_reply_destination,
    subject_for_reply,
)
from autoreplies.pipeline.strategies import PipelineStrategies, build_production_strategies
from autoreplies.services.llm import TemplateFillError
from autoreplies.services.templates import get_repeat_template_for_agent, get_template_for_agent


def _build_default_strategies() -> PipelineStrategies:
    """Build Live* strategies from settings. Called when no strategies injected."""
    from redis import Redis
    from rq import Queue

    from autoreplies.config import get_settings
    from autoreplies.services.slack import SlackClient as _SlackClient
    from autoreplies.services.supabase import SupabaseClient as _SupabaseClient

    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=redis)
    slack = _SlackClient(bot_token=settings.slack_bot_token, channel=settings.slack_channel)
    supabase = _SupabaseClient(
        url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        write_rfc822=settings.write_rfc822_message_id,
    )
    return build_production_strategies(queue=queue, slack_client=slack, supabase_client=supabase)


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
    parsed_snapshot: dict[str, Any] | None = None  # cached parsed lead
    reply_sent_message_id: str | None = None
    supabase_done: bool = False
    slack_done: bool = False
    fully_done: bool = False
    last_error: str | None = None
    attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class _NotAddressedToMailbox(Exception):
    """Internal signal: the message wasn't delivered to the polled mailbox (a
    Hiver shared-inbox mirror). process_lead skips it without side effects."""


def _addressed_to_mailbox(message: Message, mailbox_email: str) -> bool:
    """True iff `mailbox_email` is one of the message's To/Cc recipients.

    Hiver (shared inbox) copies every shared-inbox message into each Hiver user's
    *personal* mailbox while preserving the original recipients, so a mailbox can
    hold mail it was never addressed on. Gating on the actual recipient keeps a
    single shared-inbox lead auto-replied exactly once — by the addressed
    mailbox — instead of once per monitored Hiver user.
    """
    recipients = (message.get_all("To", []) or []) + (message.get_all("Cc", []) or [])
    addrs = {addr.lower() for _name, addr in getaddresses(recipients) if addr}
    return mailbox_email.lower() in addrs


def process_lead(
    message_id: str,
    mailbox_email: str,
    *,
    strategies: PipelineStrategies | None = None,
    gmail: GmailClient | None = None,
    airtable: AirtableClient | None = None,
    llm: LLMClient | None = None,
    agent_lookup_by: Literal["leads", "autoreply"] = "leads",
    dedup: DedupStore | None = None,
    dedup_window_seconds: int = 3600,
    person_resolver: PersonResolver | None = None,
    repeat_inquiry_window_seconds: int = 1209600,
    repeat_inquiry_mode: Literal["off", "observe", "enforce"] = "off",
) -> None:
    """Drive a single lead message through the full pipeline.

    Idempotent: safe to call N times for the same message_id. State checks at
    each phase ensure side-effects happen exactly once.

    `strategies` defaults to the production Live* bundle. The harness injects
    DraftSend/NoopSlack/NoopSupabase without changing this function's signature.

    `gmail`, `airtable`, `llm` are passed by the harness factory. When None,
    the phase bodies raise NotImplementedError until Phase 1 wires production
    service construction from settings/deps.

    `agent_lookup_by` selects which Users field the agent-lookup keys off:
    "leads" (production poller — Users.Leads Email) or "autoreply" (harness poller —
    Users.Autoreply Email (Agent)). Production callers may rely on the default;
    the harness passes "autoreply" explicitly from build_harness_pipeline.
    """
    if strategies is None:
        strategies = _build_default_strategies()
    if dedup is None:
        dedup = NoopDedup()
    if person_resolver is None:
        person_resolver = NoopResolver()

    state = _load_state(message_id, mailbox_email)
    if state.fully_done:
        logger.info("process_lead: skip (fully_done) message_id=%s", message_id)
        return

    state.attempts += 1
    _save_state(state)

    try:
        # Phase A — pre-Airtable: parse, reply, then Airtable insert.
        if not state.airtable_record_id:
            _phase_a_create_airtable(
                state,
                strategies,
                gmail=gmail,
                airtable=airtable,
                llm=llm,
                agent_lookup_by=agent_lookup_by,
                dedup=dedup,
                dedup_window_seconds=dedup_window_seconds,
                person_resolver=person_resolver,
                repeat_inquiry_window_seconds=repeat_inquiry_window_seconds,
                repeat_inquiry_mode=repeat_inquiry_mode,
            )
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
        logger.info(
            "process_lead: done message_id=%s record_id=%s", message_id, state.airtable_record_id
        )

    except _NotAddressedToMailbox:
        # Not a failure — a Hiver shared-inbox mirror for another mailbox. Skip
        # cleanly (no reply, no rows); the poller marks it processed so it isn't
        # re-evaluated.
        logger.info(
            "process_lead: skip (Hiver mirror; not addressed to mailbox=%s) message_id=%s",
            mailbox_email,
            message_id,
        )
        return

    except DuplicateLeadSuppressed as exc:
        logger.info(
            "process_lead: duplicate suppressed message_id=%s prior=%s",
            message_id,
            exc.prior_message_id,
        )
        return

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
    agent_lookup_by: Literal["leads", "autoreply"],
    dedup: DedupStore,
    dedup_window_seconds: int,
    person_resolver: PersonResolver,
    repeat_inquiry_window_seconds: int,
    repeat_inquiry_mode: Literal["off", "observe", "enforce"],
) -> None:
    """Fetch the email, parse, generate + send the auto-reply, write Airtable.

    On success: populates state.airtable_record_id, state.parsed_snapshot,
    state.reply_sent_message_id, and state.extra for use by phases B and C.
    """
    if gmail is None or airtable is None or llm is None:
        raise NotImplementedError("Phase 1")

    # 1. Fetch raw email.
    message, thread_id = gmail.get_message(state.message_id)

    # RFC-822 Message-ID header of the lead email — the stable, Hiver-immune key
    # message-monitor stitches conversations on (same across all Hiver copies).
    # Stored raw (brackets kept, case preserved) to match message-monitor's form;
    # written to Supabase only when the write flag is enabled (Part A foundation).
    incoming_rfc822_message_id = (message.get("Message-ID") or "").strip() or None

    # 1a. Hiver (shared inbox) drops a copy of every shared-inbox message into
    #     each Hiver user's personal mailbox, keeping the original recipients —
    #     so a mailbox holds mail it was never addressed on. Skip a message not
    #     actually addressed to the mailbox we're polling, so one shared-inbox
    #     lead is auto-replied once (by the addressed mailbox), not once per
    #     monitored Hiver user (e.g. a lead To: inbox@ otherwise also fires from
    #     jair@ because jair@ mirrors inbox@).
    if not _addressed_to_mailbox(message, state.mailbox_email):
        raise _NotAddressedToMailbox(state.mailbox_email)

    # 2. Parse the lead.
    parsed = parsers_base.parse(message)

    # 2b. Content-fingerprint dedup — suppress re-sends of the same inquiry.
    #     Checked immediately after parse, before any side effect.
    fingerprint = compute_fingerprint(
        mailbox=state.mailbox_email,
        prospect_email=parsed.email,
        message_body=parsed.message_body,
        apartment_address=parsed.apartment_address,
        source=parsed.source,
    )
    try:
        prior = dedup.recent_duplicate_message_id(
            mailbox=state.mailbox_email,
            fingerprint=fingerprint,
            exclude_message_id=state.message_id,
            within_seconds=dedup_window_seconds,
        )
    except Exception:
        logger.exception(
            "_phase_a: dedup lookup failed; failing OPEN (will reply) message_id=%s",
            state.message_id,
        )
        prior = None  # FAIL OPEN — never gate a reply on the dedup subsystem
    if prior is not None:
        logger.info(
            "_phase_a: duplicate suppressed message_id=%s prior=%s",
            state.message_id,
            prior,
        )
        raise DuplicateLeadSuppressed(fingerprint=fingerprint, prior_message_id=prior)

    # 2c. Person-identity repeated-inquiry detection (Phase 2).
    #     Resolve after Phase-1 dedup so we never call the RPC for re-sends.
    #     Gate entirely on repeat_inquiry_mode to keep Phase 2 inert by default.
    person_id: str | None = None
    is_repeat = False
    if repeat_inquiry_mode != "off":
        try:
            person_id = person_resolver.resolve_person_id(
                email=parsed.email,
                phone=parsed.phone,
            )
        except Exception:
            logger.exception(
                "_phase_a: person_id resolve failed; failing OPEN message_id=%s",
                state.message_id,
            )
            person_id = None

        if person_id is not None:
            try:
                _person_repeat = dedup.recent_person_reply(
                    person_id=person_id,
                    mailbox=state.mailbox_email,
                    exclude_message_id=state.message_id,
                    within_seconds=repeat_inquiry_window_seconds,
                )
            except Exception:
                logger.exception(
                    "_phase_a: person dedup lookup failed; failing OPEN message_id=%s",
                    state.message_id,
                )
                _person_repeat = False

            if _person_repeat:
                if repeat_inquiry_mode == "observe":
                    logger.info(
                        "_phase_a: OBSERVE repeat inquiry "
                        "person_id=%s mailbox=%s message_id=%s "
                        "(would swap to repeat template in enforce mode)",
                        person_id,
                        state.mailbox_email,
                        state.message_id,
                    )
                elif repeat_inquiry_mode == "enforce":
                    is_repeat = True

    # 3. Match apartment.
    apartment_record, apartment_match_strategy, apartment_match_confidence = (
        _match_apartment_for_lead(airtable, parsed)
    )
    apartment_record_id = apartment_record["id"] if apartment_record else None

    # 4. Match user (never create — only match existing).
    user_record = airtable.find_existing_user(email=parsed.email, phone=parsed.phone)
    user_record_id = user_record["id"] if user_record else None

    # 5. Load agent record for the mailbox. The lookup field depends on which
    #    poller drove us in: production polls Users.Leads Email; harness polls
    #    Autoreply Email (Agent). The shared call site is parameterized so the
    #    still-running harness keeps attributing agents correctly during cutover.
    if agent_lookup_by == "autoreply":
        agent_record = airtable.find_monitored_user_by_autoreply_email(state.mailbox_email)
    else:
        agent_record = airtable.find_monitored_user_by_leads_email(state.mailbox_email)
    if agent_record is None:
        logger.warning("_phase_a: no agent record found for mailbox=%s", state.mailbox_email)

    # 6. Look up reply template.
    #    Phase 2 (enforce mode): swap to the agent's repeat template when is_repeat.
    #    First-touch: harness uses autoreply_test_template; production uses
    #    autoreply_template. An empty field falls back to the Pear-wide fallback.
    schema = airtable.schema
    if is_repeat:
        template_text, template_source = get_repeat_template_for_agent(
            agent_record or {},
            template_field_id=schema.users.autoreply_repeat_template,
        )
        logger.info(
            "_phase_a: repeat template selected person_id=%s message_id=%s source=%s",
            person_id,
            state.message_id,
            template_source,
        )
    else:
        first_touch_field_id = (
            schema.users.autoreply_test_template
            if schema.users.autoreply_test_template != "MISSING"
            else schema.users.autoreply_template
        )
        template_text, template_source = get_template_for_agent(
            agent_record or {}, template_field_id=first_touch_field_id
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
    dest = resolve_reply_destination(message=message, parsed=parsed, thread_id=thread_id or None)

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
    #     LiveSend builds a GmailClient inside a scheduled RQ job using mailbox_email.
    send_result = strategies.send.send_reply(
        to=dest.recipient or "",
        subject=reply_subject,
        plaintext_body=filled_body,
        html_body=filled_body,  # Phase 2 caller adds HTML signature if needed
        in_reply_to_message_id=dest.in_reply_to_message_id,
        thread_id=dest.thread_id,
        agent=agent_record or {},
        parsed=parsed,
        inquiry_record_id=inquiry_id,
        gmail_message_id=state.message_id,
        mailbox_email=state.mailbox_email,
        reply_route=dest.route,
        skipped_reason=dest.skipped_reason,
        apartment_match_strategy=apartment_match_strategy,
        apartment_match_confidence=apartment_match_confidence,
        template_source=template_source,
        llm_model=llm_model,
        llm_latency_ms=llm_latency_ms,
        notes=notes,
    )

    # 11b. Record fingerprint for future dedup — only when we actually replied.
    #      Record at enqueue time (here), NOT at Gmail-send time: duplicates often
    #      both arrive before the humanization-delayed first send fires.
    if dest.route != "skipped":
        try:
            dedup.record_reply(
                mailbox=state.mailbox_email,
                fingerprint=fingerprint,
                gmail_message_id=state.message_id,
                inquiry_id=inquiry_id,
            )
        except Exception:
            logger.exception(
                "_phase_a: dedup record failed (reply already dispatched) message_id=%s",
                state.message_id,
            )

        # 11c. Record person reply for Phase-2 window (first-touch AND repeat).
        #      Records whenever person_id resolved — keeps the window warm for
        #      observe→enforce flip and ensures repeat detection stays accurate.
        if person_id is not None:
            try:
                dedup.record_person_reply(
                    person_id=person_id,
                    mailbox=state.mailbox_email,
                    gmail_message_id=state.message_id,
                )
            except Exception:
                logger.exception(
                    "_phase_a: person reply record failed (reply already dispatched) message_id=%s",
                    state.message_id,
                )

    # 12. Update state.
    state.airtable_record_id = inquiry_id
    state.parsed_snapshot = dataclasses.asdict(parsed)
    state.reply_sent_message_id = send_result.sent_id

    agent_fields = (agent_record.get("fields") or {}) if agent_record else {}
    prospect_parts = [p for p in (parsed.first_name, parsed.last_name) if p]
    name_form = " ".join(p for p in (parsed.first_name, parsed.last_name) if p)
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
            f"https://mail.google.com/mail/u/0/#all/{dest.thread_id}" if dest.thread_id else ""
        ),
        # Fields needed for Supabase upsert (Phase 3).
        "apartment_record_id": apartment_record_id,
        "user_record_id": user_record_id,
        "name_form": name_form or None,
        "email_form": parsed.email,
        "message": parsed.message_body,
        "type_platform": parsed.source,
        "rfc822_message_id": incoming_rfc822_message_id,
    }

    logger.info(
        "_phase_a: done message_id=%s inquiry_id=%s route=%s parser=%s",
        state.message_id,
        inquiry_id,
        dest.route,
        parsed.parser_used,
    )


def _phase_b_write_supabase(state: JobState, strategies: PipelineStrategies) -> None:
    """Upsert into Supabase using the Airtable record ID as primary key."""
    extra = state.extra
    strategies.supabase.upsert_inquiry(
        id=state.airtable_record_id or "",
        gmail_message_id=state.message_id,
        user_id=extra.get("user_record_id"),
        apartment_id=extra.get("apartment_record_id"),
        apartment_failsafe=extra.get("apartment_address"),
        name_form=extra.get("name_form"),
        email_form=extra.get("email_form"),
        name=extra.get("prospect_name"),
        email=extra.get("prospect_email"),
        phone=extra.get("prospect_phone"),
        message=extra.get("message"),
        type_platform=extra.get("type_platform", ""),
        # SupabaseClient gates whether this is actually written (write_rfc822).
        rfc822_message_id=extra.get("rfc822_message_id"),
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
    structured match (exact house_no + fuzzy street + exact unit, confidence=street_score) → none.
    """
    if parsed.listing_id:
        record = airtable.match_apartment_by_streeteasy_id(parsed.listing_id)
        if record:
            return record, "streeteasy_id", 100

    if parsed.apartment_address:
        result = airtable.match_apartment_by_address(parsed.apartment_address)
        if result:
            record, score = result
            return record, "address", score

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
