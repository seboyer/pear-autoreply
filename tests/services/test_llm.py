"""Tests for services/llm.py — fill_template + post-check + literal fallback.

The Anthropic SDK is mocked at the client level — tests never hit the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import anthropic
import pytest

from autoreplies.services.llm import (
    LLMClient,
    TemplateFillError,
    _PostCheckFailed,
    literal_fill,
)

TEMPLATE = (
    "Hi {{first_name|there}},\n\nThanks for your interest in {{apartment_address|the listing}}!\n"
)
GOOD_FILLED = "Hi Katie,\n\nThanks for your interest in 267 Clifton Pl #1A!\n"


# ── fixtures ──────────────────────────────────────────────────────────────────


def _tool_use_block(filled_body: str) -> Any:
    """Build a stand-in for the anthropic tool_use content block."""
    return SimpleNamespace(
        type="tool_use", name="fill_template", input={"filled_body": filled_body}
    )


def _mock_response(filled_body: str) -> Any:
    return SimpleNamespace(content=[_tool_use_block(filled_body)])


@pytest.fixture()
def client() -> LLMClient:
    c = LLMClient(api_key="fake-key", model="claude-haiku-4-5-20251001")
    c._client = MagicMock()
    return c


def _set_anthropic_response(client: LLMClient, body: str) -> MagicMock:
    """Wire `client._client.messages.create` to return a response with `body`."""
    mock_create = MagicMock(return_value=_mock_response(body))
    client._client.messages.create = mock_create  # type: ignore[union-attr]
    return mock_create


# ── literal_fill (no LLM) ─────────────────────────────────────────────────────


def test_literal_fill_substitutes_provided_slots() -> None:
    out = literal_fill(TEMPLATE, {"first_name": "Katie", "apartment_address": "267 Clifton Pl"})
    assert "Hi Katie," in out
    assert "267 Clifton Pl" in out
    assert "{{" not in out


def test_literal_fill_uses_defaults_when_value_missing() -> None:
    out = literal_fill(TEMPLATE, {"first_name": None, "apartment_address": None})
    assert "Hi there," in out
    assert "the listing" in out


def test_literal_fill_treats_blank_string_as_missing() -> None:
    out = literal_fill(TEMPLATE, {"first_name": "   ", "apartment_address": ""})
    assert "Hi there," in out
    assert "the listing" in out


def test_literal_fill_raises_when_required_slot_unset() -> None:
    tmpl = "Hi {{first_name}}, see {{apartment_address|the listing}}"
    with pytest.raises(TemplateFillError, match="first_name"):
        literal_fill(tmpl, {"apartment_address": "123 Main"})


def test_literal_fill_handles_no_placeholders() -> None:
    assert literal_fill("Hi there", {}) == "Hi there"


# ── fill_template happy path ──────────────────────────────────────────────────


def test_fill_template_returns_filled_body_from_llm(client: LLMClient) -> None:
    _set_anthropic_response(client, GOOD_FILLED)
    out = client.fill_template(
        template_text=TEMPLATE,
        slots={"first_name": "Katie", "apartment_address": "267 Clifton Pl #1A"},
    )
    assert out["filled_body"] == GOOD_FILLED
    assert out["strategy"] == "llm"
    assert out["model"] == "claude-haiku-4-5-20251001"
    assert int(out["latency_ms"]) >= 0


def test_fill_template_forces_tool_use(client: LLMClient) -> None:
    """The Anthropic call must pass tool_choice forcing the fill_template tool."""
    mock_create = _set_anthropic_response(client, GOOD_FILLED)
    client.fill_template(template_text=TEMPLATE, slots={})
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "fill_template"}
    assert call_kwargs["tools"][0]["name"] == "fill_template"


def test_fill_template_marks_system_prompt_for_caching(client: LLMClient) -> None:
    mock_create = _set_anthropic_response(client, GOOD_FILLED)
    client.fill_template(template_text=TEMPLATE, slots={})
    system_blocks = mock_create.call_args.kwargs["system"]
    assert any(b.get("cache_control") == {"type": "ephemeral"} for b in system_blocks)


# ── post-check failures fall back to literal_fill ─────────────────────────────


def test_fill_template_falls_back_on_residual_placeholder(client: LLMClient) -> None:
    """If the LLM leaves `{{` in the output, fall back to literal fill."""
    _set_anthropic_response(client, "Hi {{first_name}}, oops")
    out = client.fill_template(
        template_text=TEMPLATE,
        slots={"first_name": "Katie", "apartment_address": "267 Clifton Pl"},
    )
    assert out["strategy"] == "literal_fill"
    assert "{{" not in out["filled_body"]
    assert "Hi Katie" in out["filled_body"]


def test_fill_template_falls_back_when_new_url_introduced(client: LLMClient) -> None:
    """The LLM cannot inject URLs that weren't in the template."""
    _set_anthropic_response(
        client,
        "Hi Katie, see https://malicious.example.com/promo for details",
    )
    out = client.fill_template(template_text=TEMPLATE, slots={"first_name": "Katie"})
    assert out["strategy"] == "literal_fill"
    assert "malicious.example.com" not in out["filled_body"]


def test_fill_template_allows_urls_present_in_template(client: LLMClient) -> None:
    """A URL already in the template is allowed to appear in the filled body."""
    tmpl_with_url = TEMPLATE + "\nMore: https://pearnyc.com/about\n"
    filled_with_url = (
        "Hi Katie, here's https://pearnyc.com/about for more.\n"
        "Thanks for your interest in 267 Clifton Pl #1A!\n"
    )
    _set_anthropic_response(client, filled_with_url)
    out = client.fill_template(
        template_text=tmpl_with_url,
        slots={"first_name": "Katie", "apartment_address": "267 Clifton Pl #1A"},
    )
    assert out["strategy"] == "llm"


def test_fill_template_falls_back_when_length_exceeds_tolerance(client: LLMClient) -> None:
    """+/-20% chars. A 5x-length output is rejected."""
    bloated = ("Hi Katie, " + "lorem ipsum dolor " * 50).strip()
    _set_anthropic_response(client, bloated)
    out = client.fill_template(template_text=TEMPLATE, slots={"first_name": "Katie"})
    assert out["strategy"] == "literal_fill"


# ── LLM errors fall back to literal_fill ──────────────────────────────────────


def test_fill_template_falls_back_on_anthropic_error(client: LLMClient) -> None:
    client._client.messages.create = MagicMock(  # type: ignore[union-attr]
        side_effect=anthropic.APIConnectionError(request=MagicMock())
    )
    out = client.fill_template(
        template_text=TEMPLATE,
        slots={"first_name": "Katie", "apartment_address": "267 Clifton Pl"},
    )
    assert out["strategy"] == "literal_fill"
    assert "Hi Katie" in out["filled_body"]


def test_fill_template_falls_back_when_tool_missing(client: LLMClient) -> None:
    """The model returned text instead of a tool call — fall back."""
    text_block = SimpleNamespace(type="text", text="I refuse")
    client._client.messages.create = MagicMock(  # type: ignore[union-attr]
        return_value=SimpleNamespace(content=[text_block])
    )
    out = client.fill_template(
        template_text=TEMPLATE,
        slots={"first_name": "Katie", "apartment_address": "267 Clifton Pl"},
    )
    assert out["strategy"] == "literal_fill"


def test_fill_template_falls_back_when_no_api_key() -> None:
    """Defensive: an LLMClient constructed without an API key still fills via literal."""
    c = LLMClient(api_key="", model="claude-haiku-4-5-20251001")
    out = c.fill_template(
        template_text=TEMPLATE,
        slots={"first_name": "Katie", "apartment_address": "267 Clifton Pl"},
    )
    assert out["strategy"] == "literal_fill"
    assert "Hi Katie" in out["filled_body"]


# ── post-check unit tests ─────────────────────────────────────────────────────


def test_post_check_passes_for_clean_fill(client: LLMClient) -> None:
    # Method is private but worth exercising in isolation.
    reference = literal_fill(
        TEMPLATE, {"first_name": "Katie", "apartment_address": "267 Clifton Pl #1A"}
    )
    client._post_check(template_text=TEMPLATE, reference_body=reference, filled_body=GOOD_FILLED)


def test_post_check_rejects_residual_placeholder(client: LLMClient) -> None:
    with pytest.raises(_PostCheckFailed, match="placeholder"):
        client._post_check(
            template_text=TEMPLATE,
            reference_body=TEMPLATE,
            filled_body="Hi {{first_name}}!",
        )


def test_post_check_rejects_introduced_url(client: LLMClient) -> None:
    with pytest.raises(_PostCheckFailed, match="URLs"):
        client._post_check(
            template_text=TEMPLATE,
            reference_body=TEMPLATE,
            filled_body="Hi Katie, see https://evil.example.com\n",
        )


def test_post_check_rejects_length_drift(client: LLMClient) -> None:
    reference = literal_fill(
        TEMPLATE, {"first_name": "Katie", "apartment_address": "267 Clifton Pl"}
    )
    with pytest.raises(_PostCheckFailed, match="length"):
        client._post_check(template_text=TEMPLATE, reference_body=reference, filled_body="Hi")
