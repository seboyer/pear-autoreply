"""Tests for workers/send_job.py."""

from unittest.mock import MagicMock, patch


def test_send_reply_job_calls_gmail_and_airtable() -> None:
    from autoreplies.services.gmail import SentMessage
    from autoreplies.workers.send_job import send_reply_job

    fake_sent = SentMessage(
        message_id="sent-msg-id",
        plaintext_body="Hi Casey,",
        html_body="<p>Hi Casey,</p>",
        raw_rfc822=b"...",
    )

    mock_gmail = MagicMock()
    mock_gmail.send_reply.return_value = fake_sent

    mock_airtable = MagicMock()

    mock_settings = MagicMock()
    mock_settings.google_application_credentials = "/etc/sa.json"
    mock_settings.airtable_token = "pat-token"
    mock_settings.active_airtable_base_id = "appwPKlnV6YtbIjWz"

    with (
        patch("autoreplies.workers.send_job.get_settings", return_value=mock_settings),
        patch("autoreplies.workers.send_job.GmailClient", return_value=mock_gmail),
        patch("autoreplies.workers.send_job.AirtableClient", return_value=mock_airtable),
        patch("autoreplies.workers.send_job.get_schema", return_value=MagicMock()),
    ):
        send_reply_job(
            mailbox_email="agent@pearnyc.com",
            inquiry_record_id="recINQ1",
            to="casey@example.com",
            subject="Re: 123 Main St",
            plaintext_body="Hi Casey,",
            html_body="<p>Hi Casey,</p>",
            in_reply_to_message_id="<orig@gmail.com>",
            thread_id="thread-abc",
        )

    mock_gmail.send_reply.assert_called_once_with(
        to="casey@example.com",
        subject="Re: 123 Main St",
        plaintext_body="Hi Casey,",
        html_body="<p>Hi Casey,</p>",
        in_reply_to_message_id="<orig@gmail.com>",
        thread_id="thread-abc",
    )
    mock_airtable.update_inquiry_autoreply_body.assert_called_once_with(
        inquiry_record_id="recINQ1",
        plaintext_body="Hi Casey,",
        gmail_message_id="sent-msg-id",
    )
