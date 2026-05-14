"""Value-coercion helpers — port of the legacy Zapier script's normalization logic.

See `legacy/zapier_supabase_post.py` for the original. Behavior preserved:
- Empty strings become None.
- Numeric coercion strips currency symbols and commas.
- Date coercion is a passthrough (Supabase parses ISO 8601 itself).
"""

import re
from typing import Any

_CURRENCY_RE = re.compile(r"[$,]")


def to_null(value: Any) -> Any | None:
    """Empty strings → None. All other values pass through unchanged."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" else value


def to_number_or_null(value: Any) -> float | None:
    """Numeric coercion. Strips $ and commas. Returns None on empty / parse failure."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    s = _CURRENCY_RE.sub("", s)
    try:
        return float(s)
    except ValueError:
        return None


def to_date_or_null(value: Any) -> str | None:
    """Date passthrough — Supabase parses ISO strings server-side. Empty → None."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" else s
