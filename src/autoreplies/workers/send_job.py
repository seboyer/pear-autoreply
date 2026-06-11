"""RQ delayed job: send a Gmail reply and write back to Airtable + Supabase.

Enqueued by LiveSend.send_reply via rq.Queue.enqueue_at. All arguments are
primitives so RQ can pickle them without issue. Clients (GmailClient,
AirtableClient, SupabaseClient) are constructed inside the job from
get_settings().
"""

from __future__ import annotations

import logging

from autoreplies.config import get_settings
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import get_schema
from autoreplies.services.gmail import GmailClient
from autoreplies.services.supabase import SupabaseClient
from autoreplies.utils.email_format import plaintext_to_html

logger = logging.getLogger(__name__)


def send_reply_job(
    mailbox_email: str,
    inquiry_record_id: str,
    to: str,
    subject: str,
    plaintext_body: str,
    html_body: str,
    in_reply_to_message_id: str | None,
    thread_id: str | None,
) -> None:
    """Send the autoreply via Gmail and write the body + message-id back to
    both Airtable (Reply (Autoreply) + Gmail Message ID (Autoreply)) and
    Supabase (reply_message + reply_gmail_message_id)."""
    settings = get_settings()

    gmail = GmailClient(
        mailbox_email=mailbox_email,
        credentials_path=settings.google_application_credentials,
    )
    # `html_body` arrives as plaintext (the filled template); render it to
    # real HTML so the multipart/alternative HTML part keeps its line and
    # paragraph breaks instead of collapsing into one run-on paragraph.
    # Signature goes on the HTML part only — plaintext stays unsigned per the
    # FALLBACK_TEMPLATE design. A fetch failure or missing signature must never
    # block the send.
    html = plaintext_to_html(html_body)
    try:
        signature = gmail.get_default_signature_html()
    except Exception:
        logger.warning(
            "send_reply_job: signature fetch failed for mailbox=%s; sending unsigned",
            mailbox_email,
        )
        signature = None
    if signature:
        html = f"{html}<br><br>{signature}"
    sent = gmail.send_reply(
        to=to,
        subject=subject,
        plaintext_body=plaintext_body,
        html_body=html,
        in_reply_to_message_id=in_reply_to_message_id,
        thread_id=thread_id,
    )
    logger.info(
        "send_reply_job: sent mailbox=%s inquiry=%s gmail_id=%s",
        mailbox_email,
        inquiry_record_id,
        sent.message_id,
    )

    airtable = AirtableClient(
        token=settings.airtable_token,
        schema=get_schema(settings.active_airtable_base_id),
    )
    airtable.update_inquiry_autoreply_body(
        inquiry_record_id=inquiry_record_id,
        plaintext_body=sent.plaintext_body,
        gmail_message_id=sent.message_id,
    )
    logger.info("send_reply_job: airtable updated inquiry=%s", inquiry_record_id)

    supabase = SupabaseClient(
        url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
    )
    supabase.update_inquiry_reply(
        id=inquiry_record_id,
        reply_gmail_message_id=sent.message_id,
        reply_message=sent.plaintext_body,
    )
    logger.info("send_reply_job: supabase updated inquiry=%s", inquiry_record_id)
