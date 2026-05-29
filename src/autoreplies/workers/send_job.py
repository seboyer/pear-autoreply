"""RQ delayed job: send a Gmail reply and write back to Airtable.

Enqueued by LiveSend.send_reply via rq.Queue.enqueue_at. All arguments are
primitives so RQ can pickle them without issue. Clients (GmailClient,
AirtableClient) are constructed inside the job from get_settings().
"""

from __future__ import annotations

import logging

from autoreplies.config import get_settings
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import get_schema
from autoreplies.services.gmail import GmailClient

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
    """Send the autoreply via Gmail and write the body + message-id back to Airtable."""
    settings = get_settings()

    gmail = GmailClient(
        mailbox_email=mailbox_email,
        credentials_path=settings.google_application_credentials,
    )
    sent = gmail.send_reply(
        to=to,
        subject=subject,
        plaintext_body=plaintext_body,
        html_body=html_body,
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
        address_match_threshold=settings.apartment_fuzzy_match_threshold,
    )
    airtable.update_inquiry_autoreply_body(
        inquiry_record_id=inquiry_record_id,
        plaintext_body=sent.plaintext_body,
        gmail_message_id=sent.message_id,
    )
    logger.info(
        "send_reply_job: airtable updated inquiry=%s", inquiry_record_id
    )
