"""Pure address normalization and splitting utilities.

Normalization is applied symmetrically to both parsed leads and stored Airtable
addresses before comparison, so both sides see the same canonical form.
"""

import re

# Street-type abbreviation expansions (word-boundary anchored).
_ABBREVS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bave\b"), "avenue"),
    (re.compile(r"\bst\b"), "street"),
    (re.compile(r"\bpkwy\b"), "parkway"),
    (re.compile(r"\bblvd\b"), "boulevard"),
    (re.compile(r"\brd\b"), "road"),
    (re.compile(r"\bdr\b"), "drive"),
    (re.compile(r"\bpl\b"), "place"),
    (re.compile(r"\bln\b"), "lane"),
    (re.compile(r"\bct\b"), "court"),
    (re.compile(r"\bhwy\b"), "highway"),
    (re.compile(r"\bsq\b"), "square"),
    (re.compile(r"\bter\b"), "terrace"),
]

# Queens-style hyphenated house numbers: 21-06 → 2106
_QUEENS_HYPHEN = re.compile(r"\b(\d+)-(\d+)\b")
# Mac/Mc canonicalization: Macdonough → mcdonough
_MAC_PREFIX = re.compile(r"\bmac([a-z])")
# Remove non-word non-hyphen punctuation (apostrophes, periods, etc.)
_NON_WORD_NON_HYPHEN = re.compile(r"[^\w\s-]")
_WHITESPACE = re.compile(r"\s+")

# Split: ^(house_no) (street part...) (unit)$
# Lazy middle group ensures unit is the last whitespace-separated token.
_SPLIT_RE = re.compile(r"^(\d+)\s+(.+?)\s+([a-z0-9-]+)$")


def normalize_address(s: str) -> str:
    """Normalize an address for comparison.

    Applied identically to both the parsed lead address and stored Airtable full
    addresses before any matching — symmetry is what makes the fuzzy comparison work.
    """
    s = s.lower()
    # Strip ", <borough>, <state>, <zip>" tail — everything from first comma.
    s = s.split(",")[0].strip()
    # Queens-style hyphenated house numbers: 21-06 → 2106
    s = _QUEENS_HYPHEN.sub(lambda m: m.group(1) + m.group(2), s)
    # Mac/Mc canonicalization: macdonough → mcdonough
    s = _MAC_PREFIX.sub(r"mc\1", s)
    # Drop "#" prefix from unit designators
    s = s.replace("#", "")
    # Expand street-type abbreviations
    for pat, replacement in _ABBREVS:
        s = pat.sub(replacement, s)
    # Strip remaining non-word non-hyphen punctuation
    s = _NON_WORD_NON_HYPHEN.sub("", s)
    # Collapse whitespace
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def split_address(norm: str) -> tuple[str, str, str] | None:
    """Split a normalized address into (house_no, street, unit).

    Returns None when the string doesn't match the expected shape — e.g. when
    there is no unit number, or the house number is non-numeric. Callers should
    treat None as a non-matchable address rather than an error.
    """
    m = _SPLIT_RE.match(norm)
    if m is None:
        return None
    return m.group(1), m.group(2), m.group(3)
