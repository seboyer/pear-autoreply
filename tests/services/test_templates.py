"""Tests for services/templates.py — per-agent + fallback template lookup."""

from __future__ import annotations

import pytest

from autoreplies.services import templates


@pytest.fixture(autouse=True)
def _reset_fallback_cache() -> None:
    """Drop both @lru_cache entries so tests see fresh reads of both .md files."""
    templates._load_fallback_template.cache_clear()
    templates._load_repeat_fallback_template.cache_clear()


# ── get_pear_fallback_template ────────────────────────────────────────────────


def test_pear_fallback_template_contains_expected_slots() -> None:
    body = templates.get_pear_fallback_template()
    # Assert the slots are present without pinning the default copy — the sales
    # team edits the fallback wording, and that shouldn't break the suite.
    assert "{{first_name|" in body
    assert "{{apartment_address|" in body


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


def test_get_template_for_agent_unescapes_rich_text_slots() -> None:
    # Airtable rich-text fields return slot underscores backslash-escaped
    # (`{{first\_name}}`), which would otherwise break `{{slot}}` parsing.
    # get_template_for_agent normalizes it back to clean slots. Mirrors the
    # real shape of a populated `Autoreply Template (Agent)` field.
    agent = {
        "fields": {
            "fldTEMPLATE": r"Hi {{first\_name|there}}! See {{apartment\_address|my listing}}."
        }
    }
    body, source = templates.get_template_for_agent(agent, template_field_id="fldTEMPLATE")
    assert body == "Hi {{first_name|there}}! See {{apartment_address|my listing}}."
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


# ── _unescape_rich_text (Airtable rich-text Markdown un-escaping) ─────────────


def test_unescape_rich_text_unescapes_markdown_specials() -> None:
    assert templates._unescape_rich_text(r"{{first\_name|there}}") == "{{first_name|there}}"
    assert templates._unescape_rich_text(r"a\*b\_c\.d\!e") == "a*b_c.d!e"


def test_unescape_rich_text_leaves_clean_text_unchanged() -> None:
    clean = "Hi {{first_name|there}}! Thanks for your interest in {{apartment_address|my listing}}."
    assert templates._unescape_rich_text(clean) == clean


def test_unescape_rich_text_preserves_intentional_markdown() -> None:
    # No backslash to strip — bold/italic markers pass through untouched.
    assert templates._unescape_rich_text("**bold** and _italic_") == "**bold** and _italic_"


# ── reload_pear_fallback_template ─────────────────────────────────────────────


def test_reload_clears_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`reload_pear_fallback_template` re-reads the file. Simulate an edit."""
    fake_md = tmp_path / "FALLBACK_TEMPLATE.md"
    fake_md.write_text(
        "# heading\n\n## Template body\n\n> Initial body\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(templates, "_FALLBACK_PATH", fake_md)
    templates._load_fallback_template.cache_clear()

    assert templates.get_pear_fallback_template() == "Initial body"

    fake_md.write_text(
        "## Template body\n\n> Edited body\n",
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


# ── get_pear_repeat_fallback_template + get_repeat_template_for_agent (Phase 2) ─


def test_pear_repeat_fallback_template_loads_and_has_slots() -> None:
    body = templates.get_pear_repeat_fallback_template()
    assert isinstance(body, str)
    assert len(body) > 10
    assert not body.startswith(">")


def test_pear_repeat_fallback_is_separate_from_first_touch() -> None:
    """The repeat fallback must not be the same text as the first-touch fallback."""
    first_touch = templates.get_pear_fallback_template()
    repeat = templates.get_pear_repeat_fallback_template()
    assert first_touch != repeat


def test_get_repeat_template_for_agent_returns_agent_template() -> None:
    agent = {"fields": {"fldREPEAT": "Hi again {{first_name|there}}, repeat reply!"}}
    body, source = templates.get_repeat_template_for_agent(agent, template_field_id="fldREPEAT")
    assert body == "Hi again {{first_name|there}}, repeat reply!"
    assert source == "agent"


def test_get_repeat_template_for_agent_falls_back_to_repeat_fallback_not_first_touch() -> None:
    """When the repeat field is blank, falls back to the REPEAT fallback (not first-touch)."""
    agent = {"fields": {"fldREPEAT": ""}}
    body, source = templates.get_repeat_template_for_agent(agent, template_field_id="fldREPEAT")
    assert source == "pear_default"
    assert body == templates.get_pear_repeat_fallback_template()
    # Crucially, it does NOT fall back to the first-touch template.
    assert body != templates.get_pear_fallback_template()


def test_get_repeat_template_for_agent_missing_field_falls_back(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """field_id='MISSING' is treated as no template — falls back to repeat fallback."""
    agent = {"fields": {}}
    body, source = templates.get_repeat_template_for_agent(agent, template_field_id="MISSING")
    assert source == "pear_default"
    assert body == templates.get_pear_repeat_fallback_template()
