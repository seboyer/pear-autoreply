"""Supabase writer (stub).

Phase 3 ports the legacy Zapier script's logic into here. See:
- legacy/zapier_supabase_post.py for the original.
- PLAN.md § 6 for the field-mapping spec.

Critical contract: `id` is the **Airtable record ID**, not the Gmail message-id.
`user_id` and `apartment_id` are likewise Airtable record IDs.
`gmail_message_id` is a separate column for direct auditability.
"""

from typing import Any


class SupabaseClient:
    def __init__(self, url: str, service_role_key: str) -> None:
        self.url = url
        self.key = service_role_key

    def upsert_inquiry(
        self,
        *,
        id: str,
        gmail_message_id: str,
        user_id: str | None,
        apartment_id: str | None,
        apartment_failsafe: str | None,
        name_form: str | None,
        email_form: str | None,
        name: str | None,
        email: str | None,
        phone: str | None,
        message: str | None,
        type_platform: str,           # "StreetEasy" or "Zillow"
        method: str = "Web",          # PLAN.md § 6 — describes the prospect's contact channel
        date_created: str | None = None,
        sales: bool = False,          # rental-platform leads are always sales=False
        **extra: Any,                 # forward-compat for fields added later
    ) -> dict[str, Any]:
        """Upsert a row in the `inquiries` table.

        Uses `Prefer: resolution=merge-duplicates,return=representation` so retries
        with the same `id` are no-ops rather than constraint violations.
        """
        raise NotImplementedError("Phase 3")
