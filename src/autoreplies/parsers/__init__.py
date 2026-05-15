"""Source-specific email parsers.

Dispatched on the `From:` header. Public surface is `parse(message)` returning
a `ParsedLead`. See `base.py` for the shared dataclass + dispatcher.
"""

from .base import ParsedLead, ParserError, parse

__all__ = ["ParsedLead", "ParserError", "parse"]
