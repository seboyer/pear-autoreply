"""Reply destination resolution and subject helpers.

Per PLAN.md § 4 recipient priority: Reply-To header → parsed.email → skip.
Route is "thread" when a Gmail thread_id is available, "direct" otherwise.
The "skipped" route means no recipient was resolved; DraftSend still records
the Drafts row with the skipped reason so it appears in the test base for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from typing import Literal

from autoreplies.parsers.base import ParsedLead, extract_reply_to_email


@dataclass(frozen=True)
class ReplyDestination:
    route: Literal["thread", "direct", "skipped"]
    recipient: str | None
    skipped_reason: str | None
    in_reply_to_message_id: str | None
    thread_id: str | None


def resolve_reply_destination(
    *,
    message: Message,
    parsed: ParsedLead,
    thread_id: str | None,
) -> ReplyDestination:
    """Resolve recipient + route for a parsed lead email.

    - Recipient: Reply-To header takes priority; falls back to parsed.email.
    - Route "thread" when thread_id is present (typical for all well-formed leads).
    - Route "direct" when thread_id is absent (defensive; shouldn't happen in practice).
    - Route "skipped" when no recipient can be resolved.
    """
    recipient = extract_reply_to_email(message) or parsed.email
    in_reply_to = _extract_message_id_header(message)

    if not recipient:
        return ReplyDestination(
            route="skipped",
            recipient=None,
            skipped_reason="no Reply-To header and no email extracted from lead",
            in_reply_to_message_id=in_reply_to,
            thread_id=thread_id,
        )

    route: Literal["thread", "direct"] = "thread" if thread_id else "direct"
    return ReplyDestination(
        route=route,
        recipient=recipient,
        skipped_reason=None,
        in_reply_to_message_id=in_reply_to,
        thread_id=thread_id,
    )


def subject_for_reply(incoming_subject: str) -> str:
    """Return the outgoing reply subject, prepending 'Re: ' idempotently."""
    if re.match(r"re:\s*", incoming_subject, re.IGNORECASE):
        return incoming_subject
    return f"Re: {incoming_subject}"


def _extract_message_id_header(message: Message) -> str | None:
    raw = message.get("Message-ID") or message.get("Message-Id")
    return raw.strip() if raw else None
