"""Harness stats report — aggregate metrics over Drafts rows in the test base."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from autoreplies.services.airtable import AirtableClient, created_after


@dataclass
class StatsReport:
    total: int
    by_source: Counter[str]
    by_parser_used: Counter[str]
    by_apartment_match_strategy: Counter[str]
    by_template_source: Counter[str]
    by_reply_route: Counter[str]
    llm_latency_p50: float | None
    llm_latency_p95: float | None
    skipped_pct: float | None  # None when total == 0

    def format_table(self) -> str:
        lines: list[str] = []

        def _section(title: str) -> None:
            lines.append("")
            lines.append(f"  {title}")
            lines.append("  " + "-" * 38)

        def _row(label: str, value: Any) -> None:
            lines.append(f"  {label:<30} {value!s:>8}")

        lines.append("=" * 42)
        lines.append("  Harness Stats Report")
        lines.append("=" * 42)

        _section("Overview")
        _row("Total drafts", self.total)
        skipped_pct = f"{self.skipped_pct:.1f}%" if self.skipped_pct is not None else "—"
        _row("Skipped %", skipped_pct)

        _section("By Source")
        for k, v in sorted(self.by_source.items()):
            _row(k, v)

        _section("By Parser Used")
        for k, v in sorted(self.by_parser_used.items()):
            _row(k, v)

        _section("By Apartment Match Strategy")
        for k, v in sorted(self.by_apartment_match_strategy.items()):
            _row(k, v)

        _section("By Template Source")
        for k, v in sorted(self.by_template_source.items()):
            _row(k, v)

        _section("By Reply Route")
        for k, v in sorted(self.by_reply_route.items()):
            _row(k, v)

        _section("LLM Latency (ms)")
        p50 = f"{self.llm_latency_p50:.0f}" if self.llm_latency_p50 is not None else "—"
        p95 = f"{self.llm_latency_p95:.0f}" if self.llm_latency_p95 is not None else "—"
        _row("p50", p50)
        _row("p95", p95)

        lines.append("")
        lines.append("=" * 42)
        return "\n".join(lines)


class HarnessStats:
    """Compute aggregate stats over Drafts rows in the test base."""

    def __init__(self, airtable: AirtableClient) -> None:
        self._airtable = airtable

    def compute(self, since_iso: str) -> StatsReport:
        # Validate / normalise the iso string before handing to Airtable.
        datetime.fromisoformat(since_iso)

        d = self._airtable.schema.drafts
        rows = self._airtable._table(d.id).all(formula=created_after(since_iso))

        total = len(rows)
        by_source: Counter[str] = Counter()
        by_parser_used: Counter[str] = Counter()
        by_apartment_match_strategy: Counter[str] = Counter()
        by_template_source: Counter[str] = Counter()
        by_reply_route: Counter[str] = Counter()
        latencies: list[float] = []

        for row in rows:
            f = row.get("fields", {})
            by_source[f.get(d.source) or ""] += 1
            by_parser_used[f.get(d.parser_used) or ""] += 1
            by_apartment_match_strategy[f.get(d.apartment_match_strategy) or ""] += 1
            by_template_source[f.get(d.template_source) or ""] += 1
            by_reply_route[f.get(d.reply_route) or ""] += 1
            lat = f.get(d.llm_latency_ms)
            if lat is not None:
                latencies.append(float(lat))

        if latencies:
            qs = statistics.quantiles(latencies, n=20, method="exclusive")
            # n=20 → 19 quantiles; index 9 = 50th percentile, index 18 = 95th
            p50: float | None = qs[9]
            p95: float | None = qs[18]
        else:
            p50 = None
            p95 = None

        skipped = by_reply_route.get("skipped", 0)
        skipped_pct: float | None = (skipped / total * 100) if total else None

        return StatsReport(
            total=total,
            by_source=by_source,
            by_parser_used=by_parser_used,
            by_apartment_match_strategy=by_apartment_match_strategy,
            by_template_source=by_template_source,
            by_reply_route=by_reply_route,
            llm_latency_p50=p50,
            llm_latency_p95=p95,
            skipped_pct=skipped_pct,
        )
