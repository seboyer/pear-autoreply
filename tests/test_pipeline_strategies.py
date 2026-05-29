"""Tests for pipeline/strategies.py.

Verifies:
- build_production_strategies() returns the right types when given mocked clients.
- Live* classes delegate to their injected clients.
- process_lead uses injected strategies instead of building its own.
- process_lead falls back to _build_default_strategies() when no strategies given.
- Phase A body still raises NotImplementedError when services are None.
"""

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from autoreplies.pipeline.process_lead import process_lead
from autoreplies.pipeline.strategies import (
    LiveSend,
    LiveSlack,
    LiveSupabase,
    PipelineStrategies,
    SendResult,
    build_production_strategies,
)

# ---------------------------------------------------------------------------
# build_production_strategies
# ---------------------------------------------------------------------------


def _make_strategies() -> PipelineStrategies:
    queue = MagicMock()
    slack_client = MagicMock()
    supabase_client = MagicMock()
    return build_production_strategies(
        queue=queue, slack_client=slack_client, supabase_client=supabase_client
    )


def test_build_production_strategies_returns_pipeline_strategies() -> None:
    result = _make_strategies()
    assert isinstance(result, PipelineStrategies)


def test_build_production_strategies_wires_live_types() -> None:
    result = _make_strategies()
    assert isinstance(result.send, LiveSend)
    assert isinstance(result.slack, LiveSlack)
    assert isinstance(result.supabase, LiveSupabase)


# ---------------------------------------------------------------------------
# LiveSend
# ---------------------------------------------------------------------------


def test_live_send_skipped_route_returns_none_sent_id() -> None:
    queue = MagicMock()
    live = LiveSend(queue=queue)
    result = live.send_reply(
        to="prospect@example.com",
        subject="Re: 123 Main St",
        plaintext_body="Hi there,",
        html_body="<p>Hi there,</p>",
        in_reply_to_message_id=None,
        thread_id=None,
        agent={},
        parsed=MagicMock(),
        inquiry_record_id="recINQ1",
        gmail_message_id="msg-123",
        mailbox_email="agent@pearnyc.com",
        reply_route="skipped",
        skipped_reason="template_fill_error",
        apartment_match_strategy="none",
        apartment_match_confidence=None,
        template_source="pear_default",
        llm_model="claude-haiku-4-5-20251001",
        llm_latency_ms=None,
        notes="",
    )
    assert result.sent_id is None
    queue.enqueue_at.assert_not_called()


def test_live_send_enqueues_job_when_not_skipped() -> None:
    queue = MagicMock()
    fake_job = MagicMock()
    fake_job.id = "job-abc"
    queue.enqueue_at.return_value = fake_job

    live = LiveSend(queue=queue)

    with (
        patch("autoreplies.pipeline.strategies.get_settings") as mock_settings,
        patch("autoreplies.pipeline.strategies.compute_send_at") as mock_compute,
    ):
        from datetime import datetime

        mock_settings.return_value.humanization_timezone = "America/New_York"
        mock_settings.return_value.humanization_working_hours_start = 8
        mock_settings.return_value.humanization_working_hours_end = 23
        mock_settings.return_value.humanization_within_jitter_min_sec = 60
        mock_settings.return_value.humanization_within_jitter_max_sec = 300
        mock_settings.return_value.humanization_out_jitter_min_sec = 0
        mock_settings.return_value.humanization_out_jitter_max_sec = 3600
        send_at = datetime(2026, 5, 29, 14, 0, tzinfo=UTC)
        mock_compute.return_value = send_at

        result = live.send_reply(
            to="prospect@example.com",
            subject="Re: 123 Main St",
            plaintext_body="Hi there,",
            html_body="<p>Hi there,</p>",
            in_reply_to_message_id="orig-msg-id",
            thread_id="thread-123",
            agent={},
            parsed=MagicMock(),
            inquiry_record_id="recINQ1",
            gmail_message_id="msg-123",
            mailbox_email="agent@pearnyc.com",
            reply_route="thread",
            skipped_reason=None,
            apartment_match_strategy="streeteasy_id",
            apartment_match_confidence=100,
            template_source="agent",
            llm_model="claude-haiku-4-5-20251001",
            llm_latency_ms=250,
            notes="",
        )

    assert result.sent_id == "job-abc"
    queue.enqueue_at.assert_called_once()
    call_kwargs = queue.enqueue_at.call_args
    assert call_kwargs.args[0] == send_at
    assert call_kwargs.kwargs.get("mailbox_email") == "agent@pearnyc.com"
    assert call_kwargs.kwargs.get("inquiry_record_id") == "recINQ1"


# ---------------------------------------------------------------------------
# LiveSlack
# ---------------------------------------------------------------------------


def test_live_slack_post_lead_delegates_to_client() -> None:
    mock_client = MagicMock()
    mock_client.post_lead.return_value = "12345.67890"
    live = LiveSlack(client=mock_client)
    ts = live.post_lead(
        source="StreetEasy",
        agent_name="Jane",
        agent_email="jane@pearnyc.com",
        prospect_name="Casey",
        prospect_email="casey@example.com",
        prospect_phone=None,
        apartment_address="123 Main St",
        apartment_match_confidence=95,
        message_excerpt="Is this available?",
        airtable_record_id="recABC",
        gmail_thread_url="https://mail.google.com/...",
    )
    assert ts == "12345.67890"
    mock_client.post_lead.assert_called_once()


def test_live_slack_post_alert_delegates_to_client() -> None:
    mock_client = MagicMock()
    mock_client.post_alert.return_value = "99999.00001"
    live = LiveSlack(client=mock_client)
    ts = live.post_alert(summary="test alert", details={"key": "val"})
    assert ts == "99999.00001"
    mock_client.post_alert.assert_called_once_with(summary="test alert", details={"key": "val"})


# ---------------------------------------------------------------------------
# LiveSupabase
# ---------------------------------------------------------------------------


def test_live_supabase_delegates_to_client() -> None:
    mock_client = MagicMock()
    mock_client.upsert_inquiry.return_value = {"id": "recABC"}
    live = LiveSupabase(client=mock_client)
    result = live.upsert_inquiry(id="recABC", type_platform="StreetEasy")
    assert result == {"id": "recABC"}
    mock_client.upsert_inquiry.assert_called_once_with(id="recABC", type_platform="StreetEasy")


# ---------------------------------------------------------------------------
# process_lead strategy injection
# ---------------------------------------------------------------------------


def _mock_strategies() -> PipelineStrategies:
    return PipelineStrategies(
        send=MagicMock(),
        slack=MagicMock(),
        supabase=MagicMock(),
    )


def test_process_lead_uses_injected_strategies_not_default() -> None:
    """When strategies are passed, _build_default_strategies is not called."""
    strats = _mock_strategies()
    with (
        patch("autoreplies.pipeline.process_lead._build_default_strategies") as mock_build,
        pytest.raises(NotImplementedError),
    ):
        process_lead("msg-id", "agent@pearnyc.com", strategies=strats)
    mock_build.assert_not_called()


def test_process_lead_defaults_to_production_strategies() -> None:
    """When strategies is None, _build_default_strategies is called once."""
    with (
        patch(
            "autoreplies.pipeline.process_lead._build_default_strategies",
            return_value=_mock_strategies(),
        ) as mock_build,
        pytest.raises(NotImplementedError),
    ):
        process_lead("msg-id", "agent@pearnyc.com")
    mock_build.assert_called_once()


def test_process_lead_phase_bodies_still_raise_not_implemented() -> None:
    """Injecting strategies does not make phases runnable — stubs still raise."""
    with pytest.raises(NotImplementedError):
        process_lead("msg-id", "agent@pearnyc.com", strategies=_mock_strategies())


def test_send_result_dataclass() -> None:
    r = SendResult(sent_id="abc123")
    assert r.sent_id == "abc123"

    r_none = SendResult(sent_id=None)
    assert r_none.sent_id is None
