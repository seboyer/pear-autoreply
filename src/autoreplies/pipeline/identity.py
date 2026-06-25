"""PersonResolver protocol and NoopResolver for Phase-2 repeat-inquiry detection."""

from __future__ import annotations

from typing import Protocol


class PersonResolver(Protocol):
    """Read-only resolver: map (email, phone) → a shared person_id from core.

    The live implementation is SupabaseClient.resolve_person_id, which calls
    the message-monitor public.person_for_contact RPC. The default (NoopResolver)
    always returns None so Phase 2 ships inert until the RPC is live.
    """

    def resolve_person_id(self, *, email: str | None, phone: str | None) -> str | None: ...


class NoopResolver:
    """Always returns None. Used when no resolver is wired (replay, offline dev)."""

    def resolve_person_id(self, *, email: str | None, phone: str | None) -> str | None:
        return None
