"""Tests for workers/send_job.py."""

from unittest.mock import MagicMock, patch


def test_send_reply_job_calls_gmail_airtable_and_supabase() -> None:
    from autoreplies.services.gmail import SentMessage
    from autoreplies.workers.send_job import send_reply_job

    fake_sent = SentMessage(
        message_id="sent-msg-id",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body="Hi Casey,<br>\n<br>\nThanks!",
        raw_rfc822=b"...",
    )

    mock_gmail = MagicMock()
    mock_gmail.send_reply.return_value = fake_sent

    mock_airtable = MagicMock()
    mock_supabase = MagicMock()

    mock_settings = MagicMock()
    mock_settings.google_application_credentials = "/etc/sa.json"
    mock_settings.airtable_token = "pat-token"
    mock_settings.active_airtable_base_id = "appwPKlnV6YtbIjWz"
    mock_settings.supabase_url = "https://fuacxndojzybijrqdbym.supabase.co"
    mock_settings.supabase_service_role_key = "service-key"

    with (
        patch("autoreplies.workers.send_job.get_settings", return_value=mock_settings),
        patch("autoreplies.workers.send_job.GmailClient", return_value=mock_gmail),
        patch("autoreplies.workers.send_job.AirtableClient", return_value=mock_airtable),
        patch("autoreplies.workers.send_job.SupabaseClient", return_value=mock_supabase),
        patch("autoreplies.workers.send_job.get_schema", return_value=MagicMock()),
    ):
        send_reply_job(
            mailbox_email="agent@pearnyc.com",
            inquiry_record_id="recINQ1",
            to="casey@example.com",
            subject="Re: 123 Main St",
            plaintext_body="Hi Casey,\n\nThanks!",
            # html_body arrives as the plaintext filled template; send_reply_job
            # renders it to HTML (newlines → <br>) before handing to Gmail.
            html_body="Hi Casey,\n\nThanks!",
            in_reply_to_message_id="<orig@gmail.com>",
            thread_id="thread-abc",
        )

    mock_gmail.send_reply.assert_called_once_with(
        to="casey@example.com",
        subject="Re: 123 Main St",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body="Hi Casey,<br>\n<br>\nThanks!",
        in_reply_to_message_id="<orig@gmail.com>",
        thread_id="thread-abc",
    )
    mock_airtable.update_inquiry_autoreply_body.assert_called_once_with(
        inquiry_record_id="recINQ1",
        plaintext_body="Hi Casey,\n\nThanks!",
        gmail_message_id="sent-msg-id",
    )
    mock_supabase.update_inquiry_reply.assert_called_once_with(
        id="recINQ1",
        reply_gmail_message_id="sent-msg-id",
        reply_message="Hi Casey,\n\nThanks!",
    )
