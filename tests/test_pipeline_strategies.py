"""Tests for pipeline/strategies.py and the H1 strategy-injection refactor.

Verifies:
- build_production_strategies() returns the right types.
- Live* stubs raise NotImplementedError (production Phase 0 status unchanged).
- process_lead uses injected strategies instead of building its own.
- process_lead falls back to build_production_strategies() when no strategies given.
- Phase A/B/C bodies still raise NotImplementedError regardless of strategy.
"""

import pytest
from unittest.mock import MagicMock, patch

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


def test_build_production_strategies_returns_pipeline_strategies() -> None:
    result = build_production_strategies()
    assert isinstance(result, PipelineStrategies)


def test_build_production_strategies_wires_live_types() -> None:
    result = build_production_strategies()
    assert isinstance(result.send, LiveSend)
    assert isinstance(result.slack, LiveSlack)
    assert isinstance(result.supabase, LiveSupabase)


# ---------------------------------------------------------------------------
# Live* stubs raise NotImplementedError
# ---------------------------------------------------------------------------


def test_live_send_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        LiveSend().send_reply(
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
            reply_route="thread",
            skipped_reason=None,
            apartment_match_strategy="streeteasy_id",
            apartment_match_confidence=None,
            template_source="agent",
            llm_model="claude-haiku-4-5-20251001",
            llm_latency_ms=None,
            notes="",
        )


def test_live_slack_post_lead_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        LiveSlack().post_lead(
            source="StreetEasy",
            agent_name="Jane",
            agent_email="jane@pearnyc.com",
            prospect_name="Casey",
            prospect_email="casey@example.com",
            prospect_phone=None,
            apartment_address="123 Main St",
            apartment_match_confidence=None,
            message_excerpt="Is this available?",
            airtable_record_id="recABC",
            gmail_thread_url="https://mail.google.com/...",
        )


def test_live_slack_post_alert_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        LiveSlack().post_alert(summary="test", details={})


def test_live_supabase_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        LiveSupabase().upsert_inquiry(id="recABC")


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
    """When strategies are passed, build_production_strategies is not called."""
    strats = _mock_strategies()
    with patch(
        "autoreplies.pipeline.process_lead.build_production_strategies"
    ) as mock_build:
        with pytest.raises(NotImplementedError):
            process_lead("msg-id", "agent@pearnyc.com", strategies=strats)
    mock_build.assert_not_called()


def test_process_lead_defaults_to_production_strategies() -> None:
    """When strategies is None, build_production_strategies is called once."""
    with patch(
        "autoreplies.pipeline.process_lead.build_production_strategies",
        return_value=_mock_strategies(),
    ) as mock_build:
        with pytest.raises(NotImplementedError):
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
