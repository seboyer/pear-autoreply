"""Tests for services/templates.py — per-agent + fallback template lookup."""

from __future__ import annotations

import pytest

from autoreplies.services import templates


@pytest.fixture(autouse=True)
def _reset_fallback_cache() -> None:
    """Drop the @lru_cache so tests see a fresh read of FALLBACK_TEMPLATE.md."""
    templates._load_fallback_template.cache_clear()


# ── get_pear_fallback_template ────────────────────────────────────────────────


def test_pear_fallback_template_contains_expected_slots() -> None:
    body = templates.get_pear_fallback_template()
    assert "{{first_name|there}}" in body
    assert "{{apartment_address|the listing}}" in body


def test_pear_fallback_template_strips_blockquote_prefix() -> None:
    body = templates.get_pear_fallback_template()
    # No raw blockquote markers should leak into the loaded template.
    assert not body.startswith(">")
    assert "\n>" not in body


def test_pear_fallback_template_preserves_paragraph_breaks() -> None:
    body = templates.get_pear_fallback_template()
    # The .md template has multiple paragraphs separated by blank blockquote lines.
    assert "\n\n" in body


def test_pear_fallback_template_is_cached() -> None:
    # Two calls return identical strings (same object via lru_cache hit).
    a = templates.get_pear_fallback_template()
    b = templates.get_pear_fallback_template()
    assert a is b


# ── get_template_for_agent ────────────────────────────────────────────────────


def test_get_template_for_agent_returns_agent_template_when_present() -> None:
    agent = {"fields": {"fldTEMPLATE": "Hi {{first_name|there}}, custom!"}}
    body, source = templates.get_template_for_agent(agent, template_field_id="fldTEMPLATE")
    assert body == "Hi {{first_name|there}}, custom!"
    assert source == "agent"


def test_get_template_for_agent_strips_whitespace() -> None:
    agent = {"fields": {"fldTEMPLATE": "  Hi {{first_name|there}},\n  "}}
    body, source = templates.get_template_for_agent(agent, template_field_id="fldTEMPLATE")
    assert body == "Hi {{first_name|there}},"
    assert source == "agent"


def test_get_template_for_agent_falls_back_when_field_missing() -> None:
    agent = {"fields": {}}
    body, source = templates.get_template_for_agent(agent, template_field_id="fldTEMPLATE")
    assert body == templates.get_pear_fallback_template()
    assert source == "pear_default"


def test_get_template_for_agent_falls_back_when_field_blank() -> None:
    agent = {"fields": {"fldTEMPLATE": "   \n\n  "}}
    body, source = templates.get_template_for_agent(agent, template_field_id="fldTEMPLATE")
    assert body == templates.get_pear_fallback_template()
    assert source == "pear_default"


def test_get_template_for_agent_falls_back_when_field_non_string() -> None:
    # Defensive: Airtable might return a non-string for a misconfigured field.
    agent = {"fields": {"fldTEMPLATE": ["not", "a", "string"]}}
    body, source = templates.get_template_for_agent(agent, template_field_id="fldTEMPLATE")
    assert source == "pear_default"
    assert body == templates.get_pear_fallback_template()


def test_get_template_for_agent_handles_missing_fields_key() -> None:
    body, source = templates.get_template_for_agent({}, template_field_id="fldTEMPLATE")
    assert source == "pear_default"
    assert body == templates.get_pear_fallback_template()


# ── reload_pear_fallback_template ─────────────────────────────────────────────


def test_reload_clears_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`reload_pear_fallback_template` re-reads the file. Simulate an edit."""
    fake_md = tmp_path / "FALLBACK_TEMPLATE.md"
    fake_md.write_text(
        "# heading\n\n"
        "## Template body\n\n"
        "> Initial body\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(templates, "_FALLBACK_PATH", fake_md)
    templates._load_fallback_template.cache_clear()

    assert templates.get_pear_fallback_template() == "Initial body"

    fake_md.write_text(
        "## Template body\n\n"
        "> Edited body\n",
        encoding="utf-8",
    )
    # Without reload, cache still serves the old version.
    assert templates.get_pear_fallback_template() == "Initial body"

    # After reload, the new content lands.
    assert templates.reload_pear_fallback_template() == "Edited body"
    assert templates.get_pear_fallback_template() == "Edited body"


# ── parser of FALLBACK_TEMPLATE.md ────────────────────────────────────────────


def test_extract_template_body_raises_when_section_missing() -> None:
    with pytest.raises(RuntimeError, match="Template body"):
        templates._extract_template_body("# heading\n\nno template here")


def test_extract_template_body_raises_when_no_blockquote() -> None:
    md = "## Template body\n\nplain text not blockquoted\n## Next\n"
    with pytest.raises(RuntimeError, match="no blockquote"):
        templates._extract_template_body(md)
