"""Gmail API client.

Wraps the Google Gmail API with domain-wide delegation for agent mailbox
impersonation. Credentials are loaded from `settings.google_application_credentials`
(a service-account JSON file).

Scopes required on the service account's domain-wide delegation:
    https://www.googleapis.com/auth/gmail.modify          (modify + send, Phase 2)
    https://www.googleapis.com/auth/gmail.settings.basic  (sendAs for signatures, Phase 2)

The harness only needs read access today, but requesting the broader scopes now
avoids a re-delegation step when Phase 2 lands.
"""

from __future__ import annotations

import base64
import email as email_lib
from dataclasses import dataclass
from email.message import Message
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


@dataclass(frozen=True)
class MessageRef:
    """Slim reference returned by messages_list."""

    id: str
    thread_id: str


class GmailClient:
    """Wraps Gmail API operations for one agent mailbox impersonation.

    One instance per mailbox — pass mailbox_email to impersonate via domain-wide
    delegation. All API calls are scoped to that mailbox as the acting user.
    """

    def __init__(self, *, mailbox_email: str, credentials_path: str) -> None:
        self._mailbox_email = mailbox_email
        creds = (
            service_account.Credentials.from_service_account_file(
                credentials_path, scopes=_SCOPES
            ).with_subject(mailbox_email)
        )
        self._service = build("gmail", "v1", credentials=creds)

    # ── MessageLister surface ─────────────────────────────────────────────────
    # Conforms to harness.poller.MessageLister Protocol.

    def list_messages(
        self, *, query: str, max_results: int = 100
    ) -> list[tuple[str, int]]:
        """Page through all messages matching query; return (id, internal_date_ms) pairs.

        Paginates internally until exhausted. Each message requires a separate
        metadata fetch to obtain internalDate (not returned by messages.list).
        Volume is low for the harness (a few leads per day), so N+1 is acceptable.
        """
        results: list[tuple[str, int]] = []
        page_token: str | None = None

        while True:
            refs, page_token = self.messages_list(
                q=query, max_results=max_results, page_token=page_token
            )
            for ref in refs:
                meta = (
                    self._service.users()
                    .messages()
                    .get(userId=self._mailbox_email, id=ref.id, format="minimal")
                    .execute()
                )
                internal_date_ms = int(meta.get("internalDate", "0"))
                results.append((ref.id, internal_date_ms))
            if page_token is None:
                break

        return results

    # ── Core API methods ──────────────────────────────────────────────────────

    def messages_list(
        self,
        *,
        q: str,
        max_results: int = 100,
        page_token: str | None = None,
    ) -> tuple[list[MessageRef], str | None]:
        """Wrap users.messages.list; return (refs, next_page_token).

        Callers that need more control over pagination (e.g. backfill) call this
        directly. list_messages() handles pagination automatically.
        """
        kwargs: dict[str, Any] = {
            "userId": self._mailbox_email,
            "q": q,
            "maxResults": max_results,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        resp = self._service.users().messages().list(**kwargs).execute()
        refs = [
            MessageRef(id=m["id"], thread_id=m.get("threadId", ""))
            for m in resp.get("messages", [])
        ]
        return refs, resp.get("nextPageToken")

    def get_message(self, message_id: str) -> tuple[Message, str]:
        """Fetch a Gmail message; return (parsed email.message.Message, thread_id).

        Uses format='raw' so the full RFC822 bytes come back as base64url.
        Decoding with email.message_from_bytes gives the same Message type that
        parsers/base.py helpers expect.

        thread_id is the Gmail thread ID string used for In-Reply-To threading.
        """
        resp = (
            self._service.users()
            .messages()
            .get(userId=self._mailbox_email, id=message_id, format="raw")
            .execute()
        )
        thread_id: str = resp.get("threadId", "")
        # Gmail's raw field is base64url without padding; pad to be safe.
        raw = base64.urlsafe_b64decode(resp["raw"] + "==")
        msg = email_lib.message_from_bytes(raw)
        return msg, thread_id

    # ── Stubs (Phases 1-2) ────────────────────────────────────────────────────

    def list_history(
        self, start_history_id: str, label_id: str
    ) -> list[dict[str, Any]]:
        """Return new messageAdded events since start_history_id for label_id."""
        raise NotImplementedError("Phase 1")

    def get_default_signature_html(self) -> str | None:
        """Fetch the default sendAs signature HTML for this mailbox."""
        raise NotImplementedError("Phase 2")

    def send_reply(
        self,
        *,
        to: str,
        subject: str,
        plaintext_body: str,
        html_body: str,
        in_reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Send a multipart/alternative reply. Returns the sent Gmail message-id."""
        raise NotImplementedError("Phase 2")

    def renew_watch(self, label_id: str, topic_name: str) -> dict[str, Any]:
        """Re-arm users.watch for this mailbox. Called daily by the scheduler."""
        raise NotImplementedError("Phase 1")
