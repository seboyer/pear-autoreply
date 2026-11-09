"""Durable post-reply writes: non-lead skip, phase B/C independence, deferral of
transient Supabase/Slack failures to retrying RQ jobs, and failure alerts.

Background: Oct-Nov 2026 Supabase timeouts. Every reply still went out, but 95
inquiry rows, 169 reply writebacks and 344 Slack posts were lost because those
writes were never retried. Retrying must never re-run phase A (second email).
"""

from __future__ import annotations

import email
from email import policy
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from autoreplies.parsers import NotALeadError, ParserError, parse
from autoreplies.pipeline.process_lead import process_lead
from autoreplies.pipeline.strategies import LiveSlack, LiveSupabase
from autoreplies.services.supabase import SupabaseHTTPError, is_transient_supabase_error
from autoreplies.workers import writeback_jobs
from autoreplies.workers.poller import FailureAlerter
from tests.pipeline.test_process_lead import (
    _harness_strategies,
    _mock_airtable,
    _mock_gmail,
    _mock_llm,
)
from tests.workers.test_send_job import _make_mock_settings, _run_send_reply_job

TOUR_FIXTURE = "streeteasy/tour__65-saint-mark-s-avenue-2b__9.eml"


def _non_lead_message() -> email.message.Message:
    raw = (
        b"From: StreetEasy <noreply@email.streeteasy.com>\r\n"
        b"To: garland@pearnyc.com\r\n"
        b"Subject: Hotsheet: 233 New Recorded Sales\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Today's sales.\r\n"
    )
    return email.message_from_bytes(raw, policy=policy.default)


# ── Non-lead emails are a skip, not a failure ─────────────────────────────────


def test_streeteasy_non_lead_raises_not_a_lead_error_subclass() -> None:
    with pytest.raises(NotALeadError) as excinfo:
        parse(_non_lead_message())
    assert isinstance(excinfo.value, ParserError)  # existing callers unaffected


def test_process_lead_skips_non_lead_without_side_effects() -> None:
    airtable = _mock_airtable()
    gmail = MagicMock()
    gmail.get_message.return_value = (_non_lead_message(), "thread-x")
    strategies = MagicMock()

    process_lead(  # must not raise -> poller counts it as succeeded, no error row
        "gmail-nonlead",
        "garland@pearnyc.com",
        strategies=strategies,
        gmail=gmail,
        airtable=airtable,
        llm=_mock_llm(),
    )

    airtable.find_or_create_inquiry.assert_not_called()
    strategies.send.send_reply.assert_not_called()
    strategies.supabase.upsert_inquiry.assert_not_called()
    strategies.slack.post_lead.assert_not_called()


# ── Phase C runs even when phase B fails ──────────────────────────────────────


def test_phase_b_failure_still_posts_slack_then_raises() -> None:
    airtable = _mock_airtable(inquiry_id="recINQ_B")
    strategies = _harness_strategies(airtable)
    strategies.supabase = MagicMock()
    strategies.supabase.upsert_inquiry.side_effect = SupabaseHTTPError("400", status_code=400)
    strategies.slack = MagicMock()

    with pytest.raises(SupabaseHTTPError):
        process_lead(
            "gmail-b-fails",
            "garland@pearnyc.com",
            strategies=strategies,
            gmail=_mock_gmail(TOUR_FIXTURE),
            airtable=airtable,
            llm=_mock_llm(),
        )

    strategies.slack.post_lead.assert_called_once()
    assert strategies.slack.post_lead.call_args.kwargs["airtable_record_id"] == "recINQ_B"


def test_phase_c_failure_after_phase_b_success_raises() -> None:
    airtable = _mock_airtable()
    strategies = _harness_strategies(airtable)
    strategies.supabase = MagicMock()
    strategies.slack = MagicMock()
    strategies.slack.post_lead.side_effect = RuntimeError("slack down")

    with pytest.raises(RuntimeError, match="slack down"):
        process_lead(
            "gmail-c-fails",
            "garland@pearnyc.com",
            strategies=strategies,
            gmail=_mock_gmail(TOUR_FIXTURE),
            airtable=airtable,
            llm=_mock_llm(),
        )
    strategies.supabase.upsert_inquiry.assert_called_once()


# ── Transient classification ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ReadTimeout("timed out"), True),
        (httpx.ConnectError("refused"), True),
        (SupabaseHTTPError("500 57014", status_code=500), True),
        (SupabaseHTTPError("521 cloudflare", status_code=521), True),
        (SupabaseHTTPError("429", status_code=429), True),
        (SupabaseHTTPError("400 column", status_code=400), False),
        (SupabaseHTTPError("401", status_code=401), False),
        (ValueError("bug"), False),
    ],
)
def test_is_transient_supabase_error(exc: BaseException, expected: bool) -> None:
    assert is_transient_supabase_error(exc) is expected


def test_supabase_http_error_is_still_a_runtime_error() -> None:
    assert isinstance(SupabaseHTTPError("x", status_code=500), RuntimeError)


# ── LiveSupabase / LiveSlack deferral ─────────────────────────────────────────


def _enqueued(queue: MagicMock) -> tuple[Any, dict[str, Any]]:
    queue.enqueue.assert_called_once()
    call = queue.enqueue.call_args
    return call.args[0], call.kwargs


def test_live_supabase_defers_transient_failure_to_retry_job() -> None:
    client = MagicMock()
    client.upsert_inquiry.side_effect = httpx.ReadTimeout("timed out")
    queue = MagicMock()

    result = LiveSupabase(client=client, queue=queue).upsert_inquiry(
        id="recINQ1", gmail_message_id="g1", type_platform="StreetEasy"
    )

    assert result == {}
    func, kwargs = _enqueued(queue)
    assert func is writeback_jobs.upsert_inquiry_job
    assert kwargs["kwargs"] == {
        "fields": {"id": "recINQ1", "gmail_message_id": "g1", "type_platform": "StreetEasy"}
    }
    assert kwargs["retry"].max == len(writeback_jobs.RETRY_INTERVALS_SECONDS)
    assert kwargs["on_failure"] is not None


def test_live_supabase_permanent_error_raises_without_enqueue() -> None:
    client = MagicMock()
    client.upsert_inquiry.side_effect = SupabaseHTTPError("400", status_code=400)
    queue = MagicMock()
    with pytest.raises(SupabaseHTTPError):
        LiveSupabase(client=client, queue=queue).upsert_inquiry(id="recINQ1")
    queue.enqueue.assert_not_called()


def test_live_supabase_without_queue_raises() -> None:
    client = MagicMock()
    client.upsert_inquiry.side_effect = httpx.ReadTimeout("timed out")
    with pytest.raises(httpx.ReadTimeout):
        LiveSupabase(client=client).upsert_inquiry(id="recINQ1")


_SLACK_FIELDS: dict[str, Any] = {
    "source": "StreetEasy",
    "agent_name": "Garland",
    "agent_email": "garland@pearnyc.com",
    "prospect_name": "Casey",
    "prospect_email": "casey@example.com",
    "prospect_phone": None,
    "apartment_address": "65 Saint Marks Ave #2B",
    "apartment_match_confidence": 100,
    "message_excerpt": "hi",
    "airtable_record_id": "recINQ1",
    "gmail_thread_url": "",
}


def test_live_slack_defers_failure_to_retry_job() -> None:
    client = MagicMock()
    client.post_lead.side_effect = RuntimeError("ratelimited")
    queue = MagicMock()

    assert LiveSlack(client=client, queue=queue).post_lead(**_SLACK_FIELDS) == ""

    func, kwargs = _enqueued(queue)
    assert func is writeback_jobs.post_lead_job
    assert kwargs["kwargs"] == {"fields": _SLACK_FIELDS}


def test_live_slack_without_queue_raises() -> None:
    client = MagicMock()
    client.post_lead.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError):
        LiveSlack(client=client).post_lead(**_SLACK_FIELDS)


# ── send_reply_job: writeback deferral never re-sends ─────────────────────────


def _sent_gmail() -> MagicMock:
    from autoreplies.services.gmail import SentMessage

    gmail = MagicMock()
    gmail.send_reply.return_value = SentMessage(
        message_id="sent-1", plaintext_body="Hi", html_body="Hi", raw_rfc822=b""
    )
    gmail.get_default_signature_html.return_value = None
    return gmail


@pytest.mark.parametrize(
    "supabase_behaviour",
    [
        {"side_effect": httpx.ReadTimeout("timed out")},
        {"side_effect": SupabaseHTTPError("521", status_code=521)},
        {"return_value": {}},  # 0 rows matched: phase-B row not written yet
    ],
)
def test_send_job_defers_reply_writeback(supabase_behaviour: dict[str, Any]) -> None:
    gmail = _sent_gmail()
    supabase = MagicMock()
    supabase.update_inquiry_reply.configure_mock(**supabase_behaviour)

    with patch("autoreplies.workers.send_job.enqueue_writeback") as enqueue:
        _run_send_reply_job(gmail, MagicMock(), supabase, _make_mock_settings())

    gmail.send_reply.assert_called_once()  # sent exactly once; job did not raise
    enqueue.assert_called_once()
    assert enqueue.call_args.args[0] is writeback_jobs.update_inquiry_reply_job
    assert enqueue.call_args.kwargs["id"] == "recINQ1"
    assert enqueue.call_args.kwargs["reply_gmail_message_id"] == "sent-1"


def test_send_job_permanent_writeback_error_raises() -> None:
    supabase = MagicMock()
    supabase.update_inquiry_reply.side_effect = SupabaseHTTPError("400", status_code=400)
    with (
        patch("autoreplies.workers.send_job.enqueue_writeback") as enqueue,
        pytest.raises(SupabaseHTTPError),
    ):
        _run_send_reply_job(_sent_gmail(), MagicMock(), supabase, _make_mock_settings())
    enqueue.assert_not_called()


def test_send_job_success_does_not_enqueue() -> None:
    supabase = MagicMock()
    supabase.update_inquiry_reply.return_value = {"id": "recINQ1"}
    with patch("autoreplies.workers.send_job.enqueue_writeback") as enqueue:
        _run_send_reply_job(_sent_gmail(), MagicMock(), supabase, _make_mock_settings())
    enqueue.assert_not_called()


# ── Retry jobs ────────────────────────────────────────────────────────────────


def test_update_inquiry_reply_job_raises_when_row_missing() -> None:
    client = MagicMock()
    client.update_inquiry_reply.return_value = {}
    with (
        patch.object(writeback_jobs, "_supabase", return_value=client),
        pytest.raises(writeback_jobs.InquiryRowMissing),
    ):
        writeback_jobs.update_inquiry_reply_job(
            id="recINQ1",
            reply_gmail_message_id="sent-1",
            reply_message="Hi",
            reply_rfc822_message_id=None,
        )


def test_upsert_inquiry_job_forwards_fields() -> None:
    client = MagicMock()
    with patch.object(writeback_jobs, "_supabase", return_value=client):
        writeback_jobs.upsert_inquiry_job({"id": "recINQ1", "gmail_message_id": "g1"})
    client.upsert_inquiry.assert_called_once_with(id="recINQ1", gmail_message_id="g1")


@pytest.mark.parametrize(("retries_left", "alerts"), [(3, False), (0, True), (None, True)])
def test_final_failure_alert_only_when_retries_exhausted(
    retries_left: int | None, alerts: bool
) -> None:
    job = MagicMock(retries_left=retries_left, description="upsert_inquiry_job inquiry=rec1")
    with patch.object(writeback_jobs, "send_ops_alert") as send:
        writeback_jobs.alert_on_final_failure(job, None, None, RuntimeError("boom"), None)
    assert send.called is alerts


def test_send_ops_alert_noop_without_channel() -> None:
    settings = MagicMock(alert_slack_channel="", slack_bot_token="x")
    with (
        patch.object(writeback_jobs, "get_settings", return_value=settings),
        patch.object(writeback_jobs, "SlackClient") as slack,
    ):
        writeback_jobs.send_ops_alert("s", {})
    slack.assert_not_called()


def test_send_ops_alert_swallows_slack_errors() -> None:
    settings = MagicMock(alert_slack_channel="#autoreplies-alerts", slack_bot_token="x")
    with (
        patch.object(writeback_jobs, "get_settings", return_value=settings),
        patch.object(writeback_jobs, "SlackClient") as slack,
    ):
        slack.return_value.post_alert.side_effect = RuntimeError("invalid_auth")
        writeback_jobs.send_ops_alert("s", {})  # must not raise
    slack.assert_called_once_with(bot_token="x", channel="#autoreplies-alerts")


# ── Poller failure alerts ─────────────────────────────────────────────────────


def test_failure_alerter_batches_and_throttles() -> None:
    now = [1000.0]
    sent: list[dict[str, Any]] = []
    alerter = FailureAlerter(
        lambda _s, d: sent.append(d), min_interval_seconds=900, clock=lambda: now[0]
    )

    alerter.flush_if_due()  # nothing pending
    assert sent == []

    alerter.record("inbox@pearnyc.com", ["m1", "m2"])
    alerter.flush_if_due()
    assert len(sent) == 1 and sent[0]["failed dispatches"] == 2

    alerter.record("dana@pearnyc.com", ["m3"])
    now[0] += 60
    alerter.flush_if_due()  # inside the window: held, not dropped
    assert len(sent) == 1

    now[0] += 900
    alerter.flush_if_due()
    assert len(sent) == 2 and sent[1]["failed dispatches"] == 1


def test_failure_alerter_survives_alert_errors() -> None:
    def boom(_s: str, _d: dict[str, Any]) -> None:
        raise RuntimeError("slack down")

    alerter = FailureAlerter(boom)
    alerter.record("inbox@pearnyc.com", ["m1"])
    alerter.flush_if_due()  # must not raise


def test_run_forever_feeds_dispatch_failures_to_alerter(tmp_path: Any) -> None:
    from autoreplies.workers.poller import PollerConfig, run_forever
    from autoreplies.workers.poller_state import PollerState

    gmail = MagicMock()
    gmail.list_messages.return_value = [("ok-1", 2_000_000_000_000), ("bad-1", 2_000_000_000_001)]

    def dispatch(message_id: str, _mailbox: str) -> None:
        if message_id == "bad-1":
            raise RuntimeError("airtable down")

    sent: list[dict[str, Any]] = []
    with patch(
        "autoreplies.workers.poller.discover_monitored_mailboxes",
        return_value=["inbox@pearnyc.com"],
    ):
        run_forever(
            airtable=MagicMock(),
            gmail_factory=lambda _mb: gmail,
            state=PollerState(str(tmp_path / "poller.sqlite")),
            dispatch=dispatch,
            config=PollerConfig(
                interval_seconds=0,
                bootstrap_lookback_seconds=60,
                mailbox_inter_sleep_seconds=0,
                install_signal_handlers=False,
                _testing_max_iterations=1,
            ),
            alerter=FailureAlerter(lambda _s, d: sent.append(d)),
        )

    assert len(sent) == 1
    assert sent[0]["failed dispatches"] == 1
    assert "bad-1" in sent[0]["messages"]
