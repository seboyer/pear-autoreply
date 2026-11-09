"""Retrying RQ jobs for post-reply bookkeeping writes.

Once a reply is queued (process_lead phase A) or sent (send_reply_job), the
remaining writes — the Supabase inquiry row, the Slack lead post, the Supabase
reply columns — must never cause a second email. So when one of them fails
transiently it is handed to a job here, which RQ retries with backoff, instead
of re-running the pipeline.

Every job is safe to repeat: the Supabase upsert merges on the Airtable record
ID and the reply PATCH targets it. (A Slack retry can, rarely, double-post if
the first call landed but its response was lost; that's an acceptable cost.)

All job arguments are primitives so RQ can pickle them. Clients are built
inside the job from settings, as in send_job.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rq import Callback, Queue, Retry

from autoreplies.config import get_settings
from autoreplies.services.slack import SlackClient
from autoreplies.services.supabase import SupabaseClient

if TYPE_CHECKING:
    from rq.job import Job

logger = logging.getLogger(__name__)

# ~2h of attempts in total; long enough to ride out an incident like Oct-Nov
# 2026 once the database recovers, short enough that a stuck write alerts the
# same day.
RETRY_INTERVALS_SECONDS = [30, 120, 300, 900, 1800, 3600]


class InquiryRowMissing(Exception):
    """The reply PATCH matched 0 rows: the phase-B row hasn't landed yet
    (its own retry may still be pending). Raised so RQ retries the PATCH."""


def _supabase() -> SupabaseClient:
    settings = get_settings()
    return SupabaseClient(
        url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        write_rfc822=settings.write_rfc822_message_id,
    )


# ── Jobs ──────────────────────────────────────────────────────────────────────


def upsert_inquiry_job(fields: dict[str, Any]) -> None:
    """Retry of process_lead phase B. `fields` are SupabaseClient.upsert_inquiry kwargs."""
    _supabase().upsert_inquiry(**fields)
    logger.info("upsert_inquiry_job: upserted inquiry=%s", fields.get("id"))


def update_inquiry_reply_job(
    *,
    id: str,
    reply_gmail_message_id: str,
    reply_message: str,
    reply_rfc822_message_id: str | None,
) -> None:
    """Retry of send_reply_job's Supabase writeback. The email has already gone."""
    row = _supabase().update_inquiry_reply(
        id=id,
        reply_gmail_message_id=reply_gmail_message_id,
        reply_message=reply_message,
        reply_rfc822_message_id=reply_rfc822_message_id,
    )
    if not row:
        raise InquiryRowMissing(f"no public.inquiries row yet for id={id}")
    logger.info("update_inquiry_reply_job: updated inquiry=%s", id)


def post_lead_job(fields: dict[str, Any]) -> None:
    """Retry of process_lead phase C. `fields` are SlackClient.post_lead kwargs."""
    settings = get_settings()
    SlackClient(bot_token=settings.slack_bot_token, channel=settings.slack_channel).post_lead(
        **fields
    )
    logger.info("post_lead_job: posted inquiry=%s", fields.get("airtable_record_id"))


# ── Enqueue + final-failure alert ─────────────────────────────────────────────


def enqueue_writeback(
    func: Any, *, queue: Queue | None = None, description: str, **kwargs: Any
) -> Job:
    """Enqueue `func(**kwargs)` with retry/backoff and a final-failure alert.

    `queue` defaults to the "default" queue on settings.redis_url (the same
    queue the worker drains; send_reply_job has no queue handle of its own).
    Raises if Redis is unreachable — callers let that surface as a failure.
    """
    if queue is None:
        from redis import Redis

        queue = Queue("default", connection=Redis.from_url(get_settings().redis_url))
    return queue.enqueue(
        func,
        kwargs=kwargs,
        description=description,
        retry=Retry(max=len(RETRY_INTERVALS_SECONDS), interval=RETRY_INTERVALS_SECONDS),
        on_failure=Callback(alert_on_final_failure),
    )


def alert_on_final_failure(
    job: Job, connection: Any, exc_type: Any, exc_value: Any, tb: Any
) -> None:
    """RQ on_failure callback. RQ calls it after every failed attempt, before
    scheduling the retry, so only act once no retries are left."""
    if job.retries_left:
        return
    summary = "Autoreplies writeback gave up after retries"
    details = {"job": job.description or job.func_name, "error": repr(exc_value)[:300]}
    logger.error("%s: %s", summary, details)
    send_ops_alert(summary, details)


def send_ops_alert(summary: str, details: dict[str, Any]) -> None:
    """Post to ALERT_SLACK_CHANNEL when configured. Never raises."""
    settings = get_settings()
    if not settings.alert_slack_channel:
        return
    try:
        SlackClient(
            bot_token=settings.slack_bot_token, channel=settings.alert_slack_channel
        ).post_alert(summary=summary, details=details)
    except Exception:
        logger.exception("send_ops_alert: Slack post failed summary=%s", summary)
