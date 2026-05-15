"""Anthropic-backed slot-fill for reply templates.

Per PLAN.md § 4: the model is *assembling* a reply, not authoring one. The
agent's template is the source of truth; the model substitutes `{{slot}}` /
`{{slot|default}}` placeholders against the parsed lead fields and returns
the filled body. Output is forced via Anthropic tool-use so the model can't
produce free text outside the schema.

A safety post-check rejects any filled body that:
  - Still contains `{{` placeholder syntax (the model didn't finish the job),
  - Introduces a URL that wasn't already in the template,
  - Drifts in length by more than ±20% versus the template (chars).
On rejection — or on any Anthropic error — the client falls back to a strict
literal Python fill using the same `{{slot|default}}` regex (no LLM in the
loop). The literal fallback can fail too, if a required (no-default) slot is
unset; in that case `fill_template` raises so the orchestrator can record
`Skipped Reason` on the Drafts row.

Subject lines are *not* filled here. Gmail threads replies via In-Reply-To
headers, and where threading isn't possible the orchestrator copies the
incoming subject verbatim. The return shape is `{"filled_body": str}`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# Slot syntax: `{{slot}}` (required) or `{{slot|default}}` (literal default).
_SLOT_PATTERN = re.compile(
    r"\{\{\s*(?P<slot>[a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\|(?P<default>[^}]*))?\s*\}\}"
)

# Any http(s) URL — used to compare URL sets before/after fill.
_URL_PATTERN = re.compile(r"https?://[^\s)>\]\"']+")

# Tool schema forces JSON output. The model can only produce a `fill_template`
# call with a `filled_body` string; free-text content is structurally rejected.
FILL_TEMPLATE_TOOL: dict[str, Any] = {
    "name": "fill_template",
    "description": (
        "Return the reply body with every {{slot}} placeholder replaced by the "
        "corresponding value. Use the literal default when a slot value is null "
        "and a {{slot|default}} form is provided. Do not alter wording outside "
        "of slot positions. Do not introduce new URLs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "filled_body": {
                "type": "string",
                "description": "The template body with all slots substituted.",
            },
        },
        "required": ["filled_body"],
        "additionalProperties": False,
    },
}

# Per PLAN.md the safety post-check tolerates ±20% length drift versus the template.
_LENGTH_TOLERANCE = 0.20


_SYSTEM_PROMPT = (
    "You fill slot placeholders in a short rental-inquiry reply template. "
    "Each placeholder is `{{slot}}` or `{{slot|default}}`. Substitute the "
    "slot's value verbatim. When a value is null/empty and a default is given "
    "after the pipe, use the default literally. Never alter wording outside "
    "of placeholders. Never introduce URLs. Return only the filled body via "
    "the `fill_template` tool — no commentary, no subject line."
)


class TemplateFillError(Exception):
    """Raised when both the LLM and the literal-fill fallback fail.

    The orchestrator surfaces this as a `Skipped Reason` on the Drafts row
    rather than sending a half-filled reply.
    """


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        *,
        max_tokens: int = 1024,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else None

    # ── public API ────────────────────────────────────────────────────────────

    def fill_template(
        self,
        *,
        template_text: str,
        slots: dict[str, Any],
    ) -> dict[str, str]:
        """Fill `{{slot}}` / `{{slot|default}}` placeholders in `template_text`.

        Returns `{"filled_body": "...", "model": "...", "latency_ms": "...",
        "strategy": "llm" | "literal_fill"}`. The orchestrator reads
        `model`, `latency_ms`, and `strategy` for the Drafts diagnostics row;
        downstream callers can ignore them.
        """
        # The literal-fill output is both (a) the post-check reference for the
        # length comparison — slot syntax overhead means the template itself is
        # a poor proxy — and (b) the fallback if the LLM fails. Compute it once.
        try:
            literal_body = literal_fill(template_text, slots)
        except TemplateFillError:
            # A required slot has no value or default; literal-fill can't recover.
            # Re-raise so the orchestrator records a skipped reason on the Drafts row.
            raise

        try:
            filled = self._call_llm(template_text=template_text, slots=slots)
            latency_ms = filled["_latency_ms"]
            body = filled["filled_body"]
            self._post_check(
                template_text=template_text,
                reference_body=literal_body,
                filled_body=body,
            )
            return {
                "filled_body": body,
                "model": self.model,
                "latency_ms": str(latency_ms),
                "strategy": "llm",
            }
        except (anthropic.AnthropicError, _PostCheckFailed) as exc:
            logger.warning(
                "fill_template: LLM path rejected (%s); falling back to literal fill",
                exc.__class__.__name__,
            )
        except Exception:
            # Defensive: any other unexpected error (e.g. malformed tool_use
            # block, transient JSON parse) still routes through literal fill.
            logger.exception("fill_template: unexpected error; falling back to literal fill")

        return {
            "filled_body": literal_body,
            "model": self.model,
            "latency_ms": "0",
            "strategy": "literal_fill",
        }

    def extract_lead_fields(self, email_body_text: str) -> dict[str, Any]:
        """LLM fallback parser. Only invoked when source-specific regex misses
        a field that *should* be present (e.g. a StreetEasy email with no
        first_name extracted). Never used to invent fields Zillow doesn't supply.
        """
        raise NotImplementedError("Phase 2")

    # ── private helpers ───────────────────────────────────────────────────────

    def _call_llm(self, *, template_text: str, slots: dict[str, Any]) -> dict[str, Any]:
        """Call Anthropic with tool-use forcing. Returns the raw tool input plus latency."""
        if self._client is None:
            raise anthropic.AnthropicError("ANTHROPIC_API_KEY is not set")

        # System prompt is stable across calls — mark it for prompt caching to
        # cut per-call cost ~70% at our volume. Template + slots vary per lead.
        system_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user_content = (
            f"Template:\n```\n{template_text}\n```\n\n"
            f"Slots (JSON):\n```\n{json.dumps(slots, ensure_ascii=False)}\n```"
        )

        t0 = time.monotonic()
        # Anthropic's TypedDicts are strict; we build plain dicts (the wire
        # representation) and cast at the boundary. The SDK validates the
        # shape itself, so a typing-ignore here is cheaper than mirroring the
        # full TypedDict surface every time we tweak a prompt.
        response = self._client.messages.create(  # type: ignore[call-overload]
            model=self.model,
            max_tokens=self._max_tokens,
            system=system_blocks,
            tools=[FILL_TEMPLATE_TOOL],
            tool_choice={"type": "tool", "name": "fill_template"},
            messages=[{"role": "user", "content": user_content}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        tool_block = next(
            (block for block in response.content if getattr(block, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None or tool_block.name != "fill_template":
            raise anthropic.AnthropicError(
                f"model did not produce a fill_template tool call; got {response.content!r}"
            )

        tool_input = tool_block.input or {}
        filled_body = tool_input.get("filled_body")
        if not isinstance(filled_body, str):
            raise anthropic.AnthropicError(
                f"tool input missing filled_body string; got {tool_input!r}"
            )
        return {"filled_body": filled_body, "_latency_ms": latency_ms}

    def _post_check(
        self,
        *,
        template_text: str,
        reference_body: str,
        filled_body: str,
    ) -> None:
        """Raise `_PostCheckFailed` if `filled_body` violates the safety rules.

        `reference_body` is the literal-fill of the same template + slots; the
        ±20% length tolerance compares against this rather than the raw
        template, because slot-syntax overhead (e.g. `{{first_name|there}}`)
        legitimately shrinks the output by 30%+ even on a faithful fill.
        """
        if "{{" in filled_body:
            raise _PostCheckFailed("residual {{ placeholder in filled body")

        template_urls = set(_URL_PATTERN.findall(template_text))
        filled_urls = set(_URL_PATTERN.findall(filled_body))
        introduced = filled_urls - template_urls
        if introduced:
            raise _PostCheckFailed(f"filled body introduced URLs not in template: {introduced!r}")

        reference_len = len(reference_body)
        filled_len = len(filled_body)
        if reference_len:
            ratio = abs(filled_len - reference_len) / reference_len
            if ratio > _LENGTH_TOLERANCE:
                raise _PostCheckFailed(
                    f"filled body length {filled_len} differs from literal-fill "
                    f"length {reference_len} by {ratio:.1%} (limit {_LENGTH_TOLERANCE:.0%})"
                )


class _PostCheckFailed(Exception):
    """Internal signal that the LLM output failed the safety check; triggers fallback."""


def literal_fill(template_text: str, slots: dict[str, Any]) -> str:
    """Substitute `{{slot}}` / `{{slot|default}}` placeholders without an LLM.

    Used as a hard fallback when the LLM path is unavailable or rejected by
    the post-check. Raises `TemplateFillError` if a required slot (no default)
    has no value in `slots`.
    """
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        slot = match.group("slot")
        default = match.group("default")
        raw_value = slots.get(slot)
        value = raw_value.strip() if isinstance(raw_value, str) else raw_value
        if value:
            return str(value)
        if default is not None:
            return default
        missing.append(slot)
        return ""

    filled = _SLOT_PATTERN.sub(_replace, template_text)
    if missing:
        raise TemplateFillError(
            f"literal fill: required slots without values or defaults: {sorted(set(missing))}"
        )
    return filled
