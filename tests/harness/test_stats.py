"""Tests for harness/stats.py — HarnessStats + StatsReport."""

from __future__ import annotations

import argparse
import statistics
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from autoreplies.harness.stats import HarnessStats
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import TEST

# ── helpers ───────────────────────────────────────────────────────────────────

def _record(fields: dict[str, Any]) -> dict[str, Any]:
    return {"id": "recXXX", "fields": fields}


def _draft_row(
    *,
    source: str = "StreetEasy",
    parser_used: str = "regex",
    apartment_match_strategy: str = "streeteasy_id",
    template_source: str = "agent",
    reply_route: str = "thread",
    llm_latency_ms: int | None = None,
) -> dict[str, Any]:
    d = TEST.drafts
    fields: dict[str, Any] = {
        d.source: source,
        d.parser_used: parser_used,
        d.apartment_match_strategy: apartment_match_strategy,
        d.template_source: template_source,
        d.reply_route: reply_route,
    }
    if llm_latency_ms is not None:
        fields[d.llm_latency_ms] = llm_latency_ms
    return _record(fields)


def _make_airtable(rows: list[dict[str, Any]]) -> AirtableClient:
    client = MagicMock(spec=AirtableClient)
    client.schema = TEST
    tbl = MagicMock()
    tbl.all.return_value = rows
    client._table.return_value = tbl
    return client


# ── count assertions ──────────────────────────────────────────────────────────

def test_counts_by_source() -> None:
    rows = [
        _draft_row(source="StreetEasy"),
        _draft_row(source="StreetEasy"),
        _draft_row(source="Zillow"),
    ]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    assert report.by_source["StreetEasy"] == 2
    assert report.by_source["Zillow"] == 1
    assert report.total == 3


def test_counts_by_parser_used() -> None:
    rows = [
        _draft_row(parser_used="regex"),
        _draft_row(parser_used="regex"),
        _draft_row(parser_used="llm_fallback"),
    ]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    assert report.by_parser_used["regex"] == 2
    assert report.by_parser_used["llm_fallback"] == 1


def test_counts_by_apartment_match_strategy() -> None:
    rows = [
        _draft_row(apartment_match_strategy="streeteasy_id"),
        _draft_row(apartment_match_strategy="address"),
        _draft_row(apartment_match_strategy="none"),
        _draft_row(apartment_match_strategy="none"),
    ]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    assert report.by_apartment_match_strategy["streeteasy_id"] == 1
    assert report.by_apartment_match_strategy["address"] == 1
    assert report.by_apartment_match_strategy["none"] == 2


def test_counts_by_template_source() -> None:
    rows = [
        _draft_row(template_source="agent"),
        _draft_row(template_source="pear_default"),
    ]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    assert report.by_template_source["agent"] == 1
    assert report.by_template_source["pear_default"] == 1


def test_counts_by_reply_route() -> None:
    rows = [
        _draft_row(reply_route="thread"),
        _draft_row(reply_route="direct"),
        _draft_row(reply_route="skipped"),
        _draft_row(reply_route="skipped"),
    ]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    assert report.by_reply_route["thread"] == 1
    assert report.by_reply_route["direct"] == 1
    assert report.by_reply_route["skipped"] == 2


# ── latency percentiles ───────────────────────────────────────────────────────

def test_latency_percentiles_known_list() -> None:
    # [100, 200, ..., 2000] = 20 values
    latencies = list(range(100, 2100, 100))
    rows = [_draft_row(llm_latency_ms=lat) for lat in latencies]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    # statistics.quantiles([100..2000], n=20, method='exclusive'):
    # index 9 (p50) and index 18 (p95)
    qs = statistics.quantiles(latencies, n=20, method="exclusive")
    assert report.llm_latency_p50 == pytest.approx(qs[9])
    assert report.llm_latency_p95 == pytest.approx(qs[18])


def test_latency_percentiles_single_value() -> None:
    rows = [_draft_row(llm_latency_ms=500)]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    # With a single value, quantiles still works
    assert report.llm_latency_p50 is not None
    assert report.llm_latency_p95 is not None


# ── edge cases ────────────────────────────────────────────────────────────────

def test_empty_result_no_crash() -> None:
    report = HarnessStats(_make_airtable([])).compute("2026-05-01")
    assert report.total == 0
    assert report.llm_latency_p50 is None
    assert report.llm_latency_p95 is None
    assert report.skipped_pct is None
    assert sum(report.by_source.values()) == 0


def test_all_rows_missing_latency() -> None:
    rows = [_draft_row(), _draft_row(), _draft_row()]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    assert report.llm_latency_p50 is None
    assert report.llm_latency_p95 is None


def test_skipped_pct_calculation() -> None:
    rows = [
        _draft_row(reply_route="thread"),
        _draft_row(reply_route="skipped"),
        _draft_row(reply_route="skipped"),
        _draft_row(reply_route="direct"),
    ]
    report = HarnessStats(_make_airtable(rows)).compute("2026-05-01")
    assert report.skipped_pct == pytest.approx(50.0)


# ── _cmd_stats end-to-end ─────────────────────────────────────────────────────

def test_cmd_stats_exits_0_and_prints_sections(capsys: pytest.CaptureFixture[str]) -> None:
    from autoreplies.harness.runner import _cmd_stats

    rows = [
        _draft_row(source="StreetEasy", reply_route="thread", llm_latency_ms=300),
        _draft_row(source="Zillow", reply_route="skipped", llm_latency_ms=500),
    ]
    mock_client = _make_airtable(rows)

    args = argparse.Namespace(since="2026-05-01")

    # _cmd_stats does a local `from autoreplies.harness.pipeline import ...`
    # so we patch the source in the pipeline module.
    with patch("autoreplies.harness.pipeline.build_harness_airtable_client", return_value=mock_client):
        result = _cmd_stats(args)

    assert result == 0
    captured = capsys.readouterr()
    out = captured.out
    for heading in [
        "Harness Stats Report",
        "Overview",
        "By Source",
        "By Parser Used",
        "By Apartment Match Strategy",
        "By Template Source",
        "By Reply Route",
        "LLM Latency",
    ]:
        assert heading in out, f"Missing section: {heading!r}"
