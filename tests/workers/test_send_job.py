"""Tests for workers/send_job.py."""

from unittest.mock import MagicMock, patch


def _make_mock_settings() -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.google_application_credentials = "/etc/sa.json"
    mock_settings.airtable_token = "pat-token"
    mock_settings.active_airtable_base_id = "appwPKlnV6YtbIjWz"
    mock_settings.supabase_url = "https://fuacxndojzybijrqdbym.supabase.co"
    mock_settings.supabase_service_role_key = "service-key"
    mock_settings.write_rfc822_message_id = False
    return mock_settings


def _run_send_reply_job(
    mock_gmail: MagicMock,
    mock_airtable: MagicMock,
    mock_supabase: MagicMock,
    mock_settings: MagicMock,
) -> None:
    from autoreplies.workers.send_job import send_reply_job

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


def test_send_reply_job_calls_gmail_airtable_and_supabase() -> None:
    from autoreplies.services.gmail import SentMessage

    # HTML body with signature appended.
    expected_html = "Hi Casey,<br>\n<br>\nThanks!<br><br><div>—Agent Sig</div>"

    fake_sent = SentMessage(
        message_id="sent-msg-id",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body=expected_html,
        raw_rfc822=b"...",
    )

    mock_gmail = MagicMock()
    mock_gmail.send_reply.return_value = fake_sent
    mock_gmail.get_default_signature_html.return_value = "<div>—Agent Sig</div>"

    mock_airtable = MagicMock()
    mock_supabase = MagicMock()

    _run_send_reply_job(mock_gmail, mock_airtable, mock_supabase, _make_mock_settings())

    mock_gmail.send_reply.assert_called_once_with(
        to="casey@example.com",
        subject="Re: 123 Main St",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body=expected_html,
        in_reply_to_message_id="<orig@gmail.com>",
        thread_id="thread-abc",
    )
    # Airtable and Supabase receive the plaintext body (unsigned), not the HTML.
    mock_airtable.update_inquiry_autoreply_body.assert_called_once_with(
        inquiry_record_id="recINQ1",
        plaintext_body="Hi Casey,\n\nThanks!",
        gmail_message_id="sent-msg-id",
    )
    mock_supabase.update_inquiry_reply.assert_called_once_with(
        id="recINQ1",
        reply_gmail_message_id="sent-msg-id",
        reply_message="Hi Casey,\n\nThanks!",
        reply_rfc822_message_id=None,  # flag off → not captured
    )


def test_send_reply_job_captures_reply_rfc822_when_enabled() -> None:
    """With WRITE_RFC822_MESSAGE_ID on, the sent message's Message-ID is fetched
    (Gmail assigns it on send) and passed to Supabase."""
    from autoreplies.services.gmail import SentMessage

    fake_sent = SentMessage(
        message_id="sent-msg-id",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body="Hi Casey,<br>\n<br>\nThanks!",
        raw_rfc822=b"...",
    )
    mock_gmail = MagicMock()
    mock_gmail.send_reply.return_value = fake_sent
    mock_gmail.get_default_signature_html.return_value = None
    # The re-fetched sent message carries the Gmail-assigned Message-ID header.
    sent_msg = MagicMock()
    sent_msg.get.return_value = "<CAF-sent-id@mail.gmail.com>"
    mock_gmail.get_message.return_value = (sent_msg, "thread-abc")

    mock_settings = _make_mock_settings()
    mock_settings.write_rfc822_message_id = True

    mock_airtable = MagicMock()
    mock_supabase = MagicMock()

    _run_send_reply_job(mock_gmail, mock_airtable, mock_supabase, mock_settings)

    mock_gmail.get_message.assert_called_once_with("sent-msg-id")
    mock_supabase.update_inquiry_reply.assert_called_once_with(
        id="recINQ1",
        reply_gmail_message_id="sent-msg-id",
        reply_message="Hi Casey,\n\nThanks!",
        reply_rfc822_message_id="<CAF-sent-id@mail.gmail.com>",
    )


def test_send_reply_job_reply_rfc822_fails_open_on_fetch_error() -> None:
    """If re-fetching the sent message fails, the reply writeback still happens
    with reply_rfc822_message_id=None (never block on the header fetch)."""
    from autoreplies.services.gmail import SentMessage

    fake_sent = SentMessage(
        message_id="sent-msg-id",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body="Hi Casey,<br>\n<br>\nThanks!",
        raw_rfc822=b"...",
    )
    mock_gmail = MagicMock()
    mock_gmail.send_reply.return_value = fake_sent
    mock_gmail.get_default_signature_html.return_value = None
    mock_gmail.get_message.side_effect = RuntimeError("gmail unavailable")

    mock_settings = _make_mock_settings()
    mock_settings.write_rfc822_message_id = True

    mock_supabase = MagicMock()
    _run_send_reply_job(mock_gmail, MagicMock(), mock_supabase, mock_settings)

    mock_supabase.update_inquiry_reply.assert_called_once_with(
        id="recINQ1",
        reply_gmail_message_id="sent-msg-id",
        reply_message="Hi Casey,\n\nThanks!",
        reply_rfc822_message_id=None,
    )


def test_send_reply_job_no_signature_sends_unsigned() -> None:
    """When get_default_signature_html returns None, HTML body has no signature appended."""
    from autoreplies.services.gmail import SentMessage

    expected_html = "Hi Casey,<br>\n<br>\nThanks!"

    fake_sent = SentMessage(
        message_id="sent-msg-id",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body=expected_html,
        raw_rfc822=b"...",
    )

    mock_gmail = MagicMock()
    mock_gmail.send_reply.return_value = fake_sent
    mock_gmail.get_default_signature_html.return_value = None

    mock_airtable = MagicMock()
    mock_supabase = MagicMock()

    _run_send_reply_job(mock_gmail, mock_airtable, mock_supabase, _make_mock_settings())

    mock_gmail.send_reply.assert_called_once_with(
        to="casey@example.com",
        subject="Re: 123 Main St",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body=expected_html,
        in_reply_to_message_id="<orig@gmail.com>",
        thread_id="thread-abc",
    )
    # Airtable and Supabase use the plaintext body unchanged.
    mock_airtable.update_inquiry_autoreply_body.assert_called_once_with(
        inquiry_record_id="recINQ1",
        plaintext_body="Hi Casey,\n\nThanks!",
        gmail_message_id="sent-msg-id",
    )
    mock_supabase.update_inquiry_reply.assert_called_once_with(
        id="recINQ1",
        reply_gmail_message_id="sent-msg-id",
        reply_message="Hi Casey,\n\nThanks!",
        reply_rfc822_message_id=None,  # flag off → not captured
    )


def test_send_reply_job_signature_fetch_failure_sends_unsigned() -> None:
    """When get_default_signature_html raises, send still completes without signature."""
    from autoreplies.services.gmail import SentMessage

    expected_html = "Hi Casey,<br>\n<br>\nThanks!"

    fake_sent = SentMessage(
        message_id="sent-msg-id",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body=expected_html,
        raw_rfc822=b"...",
    )

    mock_gmail = MagicMock()
    mock_gmail.send_reply.return_value = fake_sent
    mock_gmail.get_default_signature_html.side_effect = Exception("API error")

    mock_airtable = MagicMock()
    mock_supabase = MagicMock()

    # Must not raise.
    _run_send_reply_job(mock_gmail, mock_airtable, mock_supabase, _make_mock_settings())

    mock_gmail.send_reply.assert_called_once_with(
        to="casey@example.com",
        subject="Re: 123 Main St",
        plaintext_body="Hi Casey,\n\nThanks!",
        html_body=expected_html,
        in_reply_to_message_id="<orig@gmail.com>",
        thread_id="thread-abc",
    )
