"""Supabase writer via direct PostgREST HTTP.

Critical contract: `id` is the **Airtable record ID**, not the Gmail message-id.
`user_id` and `apartment_id` are likewise Airtable record IDs.
Uses `Prefer: resolution=merge-duplicates,return=representation` for idempotent upserts.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SupabaseClient:
    def __init__(self, url: str, service_role_key: str) -> None:
        self.url = url.rstrip("/")
        self._headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation",
        }

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
        type_platform: str,  # "StreetEasy" or "Zillow"
        method: str = "Web",  # always "Web" — describes the prospect's contact channel
        date_created: str | None = None,
        sales: bool = False,  # rental-platform leads are always sales=False
        **extra: Any,  # forward-compat for fields added later
    ) -> dict[str, Any]:
        """Upsert a row in the `inquiries` table.

        Null values are omitted so blank Airtable fields don't clobber existing
        numeric/date columns (mirrors the legacy Zapier script's behaviour).
        """
        payload: dict[str, Any] = {
            "id": id,
            "gmail_message_id": gmail_message_id,
            "type_platform": type_platform,
            "method": method,
            "sales": sales,
        }
        for key, val in [
            ("user_id", user_id),
            ("apartment_id", apartment_id),
            ("apartment_failsafe", apartment_failsafe),
            ("name_form", name_form),
            ("email_form", email_form),
            ("name", name),
            ("email", email),
            ("phone", phone),
            ("message", message),
            ("date_created", date_created),
        ]:
            if val is not None:
                payload[key] = val

        resp = httpx.post(
            f"{self.url}/rest/v1/inquiries",
            headers=self._headers,
            json=payload,
            timeout=30.0,
        )
        if not resp.is_success:
            raise RuntimeError(f"Supabase upsert failed {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        return body[0] if isinstance(body, list) else body

    def update_inquiry_reply(
        self,
        *,
        id: str,
        reply_gmail_message_id: str,
        reply_message: str,
    ) -> dict[str, Any]:
        """Write the sent reply's Gmail message-id and plaintext body back to the
        Supabase inquiry row after a successful Gmail send.

        `id` is the Airtable record ID — the row's primary key. The row is
        expected to already exist (created by the earlier `upsert_inquiry` call
        in Phase B). If 0 rows match, logs a WARNING — the Airtable row still
        has the data, so this is recoverable but worth surfacing.

        Mirrors AirtableClient.update_inquiry_autoreply_body. Called by
        send_reply_job after Gmail send completes.
        """
        resp = httpx.patch(
            f"{self.url}/rest/v1/inquiries",
            headers=self._headers,
            params={"id": f"eq.{id}"},
            json={
                "reply_gmail_message_id": reply_gmail_message_id,
                "reply_message": reply_message,
            },
            timeout=30.0,
        )
        if not resp.is_success:
            raise RuntimeError(
                f"Supabase reply update failed {resp.status_code}: {resp.text[:500]}"
            )
        body = resp.json()
        rows = body if isinstance(body, list) else [body]
        if not rows:
            logger.warning(
                "update_inquiry_reply: 0 rows matched id=%s — reply data only in Airtable",
                id,
            )
            return {}
        return rows[0]
