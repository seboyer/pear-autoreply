"""Tests for services/supabase.py — SupabaseClient."""

from unittest.mock import MagicMock, patch

import pytest

from autoreplies.services.supabase import SupabaseClient


def _client() -> SupabaseClient:
    return SupabaseClient(
        url="https://test.supabase.co",
        service_role_key="test-key",
    )


def test_upsert_inquiry_posts_correct_payload() -> None:
    client = _client()
    fake_resp = MagicMock()
    fake_resp.is_success = True
    fake_resp.json.return_value = [{"id": "recABC"}]

    with patch("autoreplies.services.supabase.httpx.post", return_value=fake_resp) as mock_post:
        result = client.upsert_inquiry(
            id="recABC",
            gmail_message_id="msg-123",
            user_id=None,
            apartment_id="recAPT1",
            apartment_failsafe="123 Main St",
            name_form="Casey Smith",
            email_form="casey@example.com",
            name="Casey Smith",
            email="casey@example.com",
            phone=None,
            message="Is this available?",
            type_platform="StreetEasy",
        )

    assert result == {"id": "recABC"}
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["id"] == "recABC"
    assert payload["gmail_message_id"] == "msg-123"
    assert payload["type_platform"] == "StreetEasy"
    assert payload["method"] == "Web"
    assert payload["sales"] is False
    assert "apartment_id" in payload
    # None values are omitted
    assert "user_id" not in payload
    assert "phone" not in payload


def test_update_inquiry_reply_patches_correct_payload() -> None:
    """update_inquiry_reply writes reply_gmail_message_id and reply_message
    to the row keyed by id, via PATCH /inquiries?id=eq.<id>."""
    client = _client()
    fake_resp = MagicMock()
    fake_resp.is_success = True
    fake_resp.json.return_value = [{"id": "recABC", "reply_message": "Hi Casey,"}]

    with patch("autoreplies.services.supabase.httpx.patch", return_value=fake_resp) as mock_patch:
        result = client.update_inquiry_reply(
            id="recABC",
            reply_gmail_message_id="sent-msg-id",
            reply_message="Hi Casey,",
        )

    assert result == {"id": "recABC", "reply_message": "Hi Casey,"}
    mock_patch.assert_called_once()
    call_kwargs = mock_patch.call_args.kwargs
    assert call_kwargs["params"] == {"id": "eq.recABC"}
    payload = call_kwargs["json"]
    assert payload == {
        "reply_gmail_message_id": "sent-msg-id",
        "reply_message": "Hi Casey,",
    }


def test_update_inquiry_reply_warns_when_row_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If 0 rows match, log a WARNING and return {} — the Airtable row still
    has the reply, so this is recoverable but worth surfacing."""
    client = _client()
    fake_resp = MagicMock()
    fake_resp.is_success = True
    fake_resp.json.return_value = []

    with (
        patch("autoreplies.services.supabase.httpx.patch", return_value=fake_resp),
        caplog.at_level("WARNING"),
    ):
        result = client.update_inquiry_reply(
            id="recMISSING",
            reply_gmail_message_id="sent-msg-id",
            reply_message="Hi Casey,",
        )

    assert result == {}
    assert any("0 rows matched" in r.message and "recMISSING" in r.message for r in caplog.records)


def test_update_inquiry_reply_raises_on_http_error() -> None:
    client = _client()
    fake_resp = MagicMock()
    fake_resp.is_success = False
    fake_resp.status_code = 401
    fake_resp.text = "unauthorized"

    with (
        patch("autoreplies.services.supabase.httpx.patch", return_value=fake_resp),
        pytest.raises(RuntimeError, match="Supabase reply update failed 401"),
    ):
        client.update_inquiry_reply(
            id="recABC",
            reply_gmail_message_id="sent-msg-id",
            reply_message="Hi Casey,",
        )


def test_upsert_inquiry_uses_merge_duplicates_header() -> None:
    client = _client()
    fake_resp = MagicMock()
    fake_resp.is_success = True
    fake_resp.json.return_value = [{"id": "recABC"}]

    with patch("autoreplies.services.supabase.httpx.post", return_value=fake_resp) as mock_post:
        client.upsert_inquiry(
            id="recABC",
            gmail_message_id="msg-1",
            user_id=None,
            apartment_id=None,
            apartment_failsafe=None,
            name_form=None,
            email_form=None,
            name=None,
            email=None,
            phone=None,
            message=None,
            type_platform="Zillow",
        )

    headers = mock_post.call_args.kwargs["headers"]
    assert "resolution=merge-duplicates" in headers["Prefer"]


def test_upsert_inquiry_raises_on_http_error() -> None:
    client = _client()
    fake_resp = MagicMock()
    fake_resp.is_success = False
    fake_resp.status_code = 400
    fake_resp.text = "Bad request"

    with (
        patch("autoreplies.services.supabase.httpx.post", return_value=fake_resp),
        pytest.raises(RuntimeError, match="Supabase upsert failed"),
    ):
        client.upsert_inquiry(
            id="recABC",
            gmail_message_id="msg-1",
            user_id=None,
            apartment_id=None,
            apartment_failsafe=None,
            name_form=None,
            email_form=None,
            name=None,
            email=None,
            phone=None,
            message=None,
            type_platform="StreetEasy",
        )


def test_upsert_inquiry_posts_to_correct_url() -> None:
    client = _client()
    fake_resp = MagicMock()
    fake_resp.is_success = True
    fake_resp.json.return_value = [{"id": "recABC"}]

    with patch("autoreplies.services.supabase.httpx.post", return_value=fake_resp) as mock_post:
        client.upsert_inquiry(
            id="recABC",
            gmail_message_id="m",
            user_id=None,
            apartment_id=None,
            apartment_failsafe=None,
            name_form=None,
            email_form=None,
            name=None,
            email=None,
            phone=None,
            message=None,
            type_platform="StreetEasy",
        )

    url = mock_post.call_args.args[0]
    assert url == "https://test.supabase.co/rest/v1/inquiries"
