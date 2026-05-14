"""SQLite-backed dedup and mailbox-cursor state for the harness poller.

Single file, no service dependency. Production uses Redis-backed state in
process_lead; the harness keeps its own cursor separately so the two never
interact. Wipe by deleting the file or running `make harness-reset`.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox_state (
    mailbox_email                TEXT PRIMARY KEY,
    last_seen_internal_date_ms   INTEGER NOT NULL,
    updated_at                   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    gmail_message_id    TEXT PRIMARY KEY,
    mailbox_email       TEXT NOT NULL,
    airtable_inquiry_id TEXT,
    airtable_draft_id   TEXT,
    processed_at        TEXT NOT NULL,
    error               TEXT
);
"""


class HarnessState:
    """Lightweight SQLite wrapper for harness polling state.

    Thread-safety: single-writer, single-reader (the poller is single-threaded).
    WAL mode enabled so reads don't block writes if we ever add a reader process.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── mailbox cursor ────────────────────────────────────────────────────────

    def get_last_seen(self, mailbox: str) -> int | None:
        """Return last-seen internalDate unix-ms for `mailbox`, or None."""
        row = self._conn.execute(
            "SELECT last_seen_internal_date_ms FROM mailbox_state WHERE mailbox_email = ?",
            (mailbox,),
        ).fetchone()
        return row[0] if row else None

    def set_last_seen(self, mailbox: str, internal_date_ms: int) -> None:
        """Upsert the last-seen internalDate cursor for `mailbox`."""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO mailbox_state (mailbox_email, last_seen_internal_date_ms, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(mailbox_email) DO UPDATE SET
                last_seen_internal_date_ms = excluded.last_seen_internal_date_ms,
                updated_at = excluded.updated_at
            """,
            (mailbox, internal_date_ms, now),
        )
        self._conn.commit()

    # ── message dedup ─────────────────────────────────────────────────────────

    def was_processed(self, message_id: str) -> bool:
        """Return True if this Gmail message-id has been processed (successfully or not)."""
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE gmail_message_id = ?",
            (message_id,),
        ).fetchone()
        return row is not None

    def mark_processed(
        self,
        message_id: str,
        mailbox: str,
        inquiry_id: str | None = None,
        draft_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record that `message_id` has been processed (or attempted)."""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_messages
                (gmail_message_id, mailbox_email, airtable_inquiry_id,
                 airtable_draft_id, processed_at, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, mailbox, inquiry_id, draft_id, now, error),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
