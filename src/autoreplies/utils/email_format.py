"""Plaintext → HTML rendering for outbound reply bodies."""

from __future__ import annotations

import html


def plaintext_to_html(text: str) -> str:
    """Render a plaintext reply body as simple, email-safe HTML.

    Mail clients render the HTML part of a ``multipart/alternative`` message in
    preference to the plain-text part, and HTML collapses runs of whitespace —
    so a plaintext body dropped verbatim into the HTML part loses every line and
    paragraph break and arrives as one run-on paragraph. This escapes the HTML
    special characters and turns newlines into ``<br>`` so the body's structure
    survives. Quotes/apostrophes are left as-is (``quote=False``) for readable
    prose; intentional Markdown such as ``**bold**`` is untouched.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return html.escape(normalized, quote=False).replace("\n", "<br>\n")
