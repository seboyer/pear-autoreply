"""Tests for services/gmail.py — GmailClient with mocked Google API."""

from base64 import urlsafe_b64encode
from unittest.mock import MagicMock, patch

import pytest

from autoreplies.services.gmail import _SCOPES, GmailClient, MessageRef

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_raw_b64(subject: str = "Test Subject", body: str = "Hello") -> str:
    """Build a minimal RFC822 message and base64url-encode it (no padding)."""
    raw = f"Subject: {subject}\r\nFrom: sender@example.com\r\n\r\n{body}".encode()
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture()
def mock_service() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def gmail_client(mock_service: MagicMock) -> GmailClient:
    """GmailClient with the Google API service replaced by a mock."""
    with (
        patch("autoreplies.services.gmail.build", return_value=mock_service),
        patch(
            "autoreplies.services.gmail.service_account.Credentials"
            ".from_service_account_file",
            return_value=MagicMock(),
        ),
    ):
        client = GmailClient(
            mailbox_email="agent@pearnyc.com",
            credentials_path="/fake/sa.json",
        )
    # Swap in the mock directly so subsequent calls route through it.
    client._service = mock_service
    return client


# ── __init__ / delegation ─────────────────────────────────────────────────────


def test_init_uses_domain_wide_delegation() -> None:
    mock_creds = MagicMock()
    mock_delegated = MagicMock()
    mock_creds.with_subject.return_value = mock_delegated
    mock_service = MagicMock()

    with (
        patch(
            "autoreplies.services.gmail.service_account.Credentials"
            ".from_service_account_file",
            return_value=mock_creds,
        ) as mock_from_file,
        patch("autoreplies.services.gmail.build", return_value=mock_service) as mock_build,
    ):
        GmailClient(mailbox_email="agent@pearnyc.com", credentials_path="/path/sa.json")

    mock_from_file.assert_called_once_with("/path/sa.json", scopes=_SCOPES)
    mock_creds.with_subject.assert_called_once_with("agent@pearnyc.com")
    mock_build.assert_called_once_with("gmail", "v1", credentials=mock_delegated)


# ── messages_list ─────────────────────────────────────────────────────────────


def test_messages_list_returns_refs_and_next_page_token(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().list().execute.return_value = {
        "messages": [
            {"id": "msg-1", "threadId": "thread-1"},
            {"id": "msg-2", "threadId": "thread-2"},
        ],
        "nextPageToken": "token-abc",
    }

    refs, next_token = gmail_client.messages_list(q="from:se", max_results=50)

    assert refs == [
        MessageRef(id="msg-1", thread_id="thread-1"),
        MessageRef(id="msg-2", thread_id="thread-2"),
    ]
    assert next_token == "token-abc"


def test_messages_list_empty_response_returns_empty_list(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().list().execute.return_value = {}

    refs, next_token = gmail_client.messages_list(q="from:se")

    assert refs == []
    assert next_token is None


def test_messages_list_passes_page_token(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m", "threadId": "t"}],
    }

    gmail_client.messages_list(q="x", max_results=10, page_token="tok-123")

    # Extract the kwargs passed to .list()
    list_call_kwargs = mock_service.users().messages().list.call_args.kwargs
    assert list_call_kwargs["pageToken"] == "tok-123"
    assert list_call_kwargs["userId"] == "agent@pearnyc.com"
    assert list_call_kwargs["q"] == "x"
    assert list_call_kwargs["maxResults"] == 10


def test_messages_list_omits_page_token_when_none(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().list().execute.return_value = {}

    gmail_client.messages_list(q="x")

    list_call_kwargs = mock_service.users().messages().list.call_args.kwargs
    assert "pageToken" not in list_call_kwargs


# ── list_messages ─────────────────────────────────────────────────────────────


def test_list_messages_paginates_and_fetches_metadata(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    # Two pages of results.
    mock_list = mock_service.users().messages().list
    mock_list.return_value.execute.side_effect = [
        {"messages": [{"id": "msg-1", "threadId": "t1"}], "nextPageToken": "p2"},
        {"messages": [{"id": "msg-2", "threadId": "t2"}]},
    ]
    mock_get = mock_service.users().messages().get
    mock_get.return_value.execute.side_effect = [
        {"internalDate": "1700000001000"},
        {"internalDate": "1700000002000"},
    ]

    results = gmail_client.list_messages(query="from:x", max_results=100)

    assert results == [("msg-1", 1_700_000_001_000), ("msg-2", 1_700_000_002_000)]


def test_list_messages_empty_returns_empty_list(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().list.return_value.execute.return_value = {}

    results = gmail_client.list_messages(query="from:x")

    assert results == []


def test_list_messages_uses_minimal_format_for_metadata(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().list.return_value.execute.return_value = {
        "messages": [{"id": "msg-1", "threadId": "t1"}],
    }
    mock_service.users().messages().get.return_value.execute.return_value = {
        "internalDate": "1000",
    }

    gmail_client.list_messages(query="x")

    get_call_kwargs = mock_service.users().messages().get.call_args.kwargs
    assert get_call_kwargs["format"] == "minimal"
    assert get_call_kwargs["id"] == "msg-1"
    assert get_call_kwargs["userId"] == "agent@pearnyc.com"


# ── get_message ───────────────────────────────────────────────────────────────


def test_get_message_returns_parsed_message_and_thread_id(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    raw_b64 = _make_raw_b64(subject="65 Saint Marks Ave StreetEasy Inquiry From Jane Doe")
    mock_service.users().messages().get().execute.return_value = {
        "id": "msg-99",
        "threadId": "thread-abc123",
        "raw": raw_b64,
    }

    msg, thread_id = gmail_client.get_message("msg-99")

    assert thread_id == "thread-abc123"
    assert msg.get("Subject") == "65 Saint Marks Ave StreetEasy Inquiry From Jane Doe"
    assert msg.get("From") == "sender@example.com"


def test_get_message_uses_raw_format(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().get().execute.return_value = {
        "threadId": "t",
        "raw": _make_raw_b64(),
    }

    gmail_client.get_message("msg-1")

    get_call_kwargs = mock_service.users().messages().get.call_args.kwargs
    assert get_call_kwargs["format"] == "raw"
    assert get_call_kwargs["id"] == "msg-1"
    assert get_call_kwargs["userId"] == "agent@pearnyc.com"


def test_get_message_handles_unpadded_base64(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    """base64url from Gmail may lack padding — get_message must not crash."""
    raw = b"Subject: Hi\r\n\r\nbody"
    # Deliberately strip padding to simulate Gmail's output.
    raw_b64 = urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    mock_service.users().messages().get().execute.return_value = {
        "threadId": "t",
        "raw": raw_b64,
    }

    msg, _ = gmail_client.get_message("msg-1")
    assert msg.get("Subject") == "Hi"


def test_get_message_empty_thread_id_defaults_to_empty_string(
    gmail_client: GmailClient, mock_service: MagicMock
) -> None:
    mock_service.users().messages().get().execute.return_value = {
        "raw": _make_raw_b64(),
        # No threadId key.
    }

    _, thread_id = gmail_client.get_message("msg-1")
    assert thread_id == ""


# ── stubs still raise ─────────────────────────────────────────────────────────


def test_list_history_raises_not_implemented(gmail_client: GmailClient) -> None:
    with pytest.raises(NotImplementedError, match="Phase 1"):
        gmail_client.list_history("h-id", "label-id")


def test_get_default_signature_raises_not_implemented(gmail_client: GmailClient) -> None:
    with pytest.raises(NotImplementedError, match="Phase 2"):
        gmail_client.get_default_signature_html()


def test_send_reply_raises_not_implemented(gmail_client: GmailClient) -> None:
    with pytest.raises(NotImplementedError, match="Phase 2"):
        gmail_client.send_reply(
            to="x@y.com",
            subject="Re: test",
            plaintext_body="hi",
            html_body="<p>hi</p>",
        )
