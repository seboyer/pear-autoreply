"""Source-specific email parsers.

Dispatched on the `From:` header. Public surface is `parse(message)` returning
a `ParsedLead`. See `base.py` for the shared dataclass + dispatcher.
"""

from .base import NotALeadError, ParsedLead, ParserError, parse

__all__ = ["NotALeadError", "ParsedLead", "ParserError", "parse"]
