# Hiver dedup comparison: autoreplies vs message-monitor

> **Status:** Q4 handoff document. Cross-link: [DEDUPE_PHASE2_HANDOFF.md](./DEDUPE_PHASE2_HANDOFF.md) (if it exists) or the Phase-2 PR description.

## Problem statement

Hiver shared-inbox distributes a copy of every shared-inbox email into each Hiver user's *personal* Gmail mailbox while preserving the original SMTP recipients. This means a single inbound StreetEasy/Zillow lead addressed to `inbox@pearnyc.com` can appear in three mailboxes simultaneously (garland@, jair@, inbox@). Without mitigation, both systems could process and act on it multiple times.

## How autoreplies handles Hiver

**Step 1 — Recipient gate (PR #25):** `_addressed_to_mailbox` in `pipeline/process_lead.py` checks the To/Cc headers against the mailbox being polled. A copy delivered into garland@'s personal mailbox that was originally addressed only to jair@ is skipped cleanly (no reply, no row). This ensures one shared-inbox lead fires from exactly one mailbox — the one it was actually addressed to.

**Step 2 — Content fingerprint (Phase 1, PR #27):** After the recipient gate, `compute_fingerprint` hashes `(mailbox, prospect_email, message_body, apartment_address, source)` and stores the hash in `replied_fingerprints`. A second Gmail message with the same hash arriving within 1 hour (e.g. a StreetEasy re-notification) is suppressed.

**Step 3 — Person-keyed repeat detection (Phase 2):** `person_resolver.resolve_person_id` maps the prospect's email/phone to a shared `person_id` from `core.person_identifiers`. If the same person contacts the same agent mailbox within 14 days, the agent's `Autoreply Repeat Template` is sent instead of the first-touch template. The Inquiry row, Supabase write, and Slack notification still happen — only the template changes.

### Limitations of the autoreplies approach

- **Keyed on parsed content**, not the RFC-822 `Message-ID` header. Two Hiver copies of the same email have the same Message-ID but *different* Gmail message IDs; the content fingerprint handles them correctly (same hash → suppress), but the dedup window (1 hour) means a second copy arriving >1 hour later would slip through to a new reply. In practice Hiver delivers copies within seconds; the window is more than sufficient.
- **No cross-mailbox dedup:** the fingerprint is scoped to `(mailbox, ...)`. If two separate agents both receive a direct email (not via Hiver), each will auto-reply independently — correct behavior for direct leads, but worth noting.

## How message-monitor handles Hiver

message-monitor deduplicates at ingest using the RFC-822 `Message-ID` header. Every email carries a globally unique `Message-ID` assigned by the sending MTA; Hiver copies of the same email share the same `Message-ID`. message-monitor stores one `core.conversations` row per `Message-ID`, so it naturally sees a Hiver-distributed email as one logical event regardless of how many mailboxes received it.

### Why the approaches differ

| | autoreplies | message-monitor |
|---|---|---|
| **Primary concern** | *Choosing the right sending mailbox* for the reply | *Observing* the inbound conversation |
| **Dedup key** | Content fingerprint scoped to a mailbox | RFC-822 `Message-ID` (global, per email) |
| **Hiver resolution** | Recipient gate (pick the addressed mailbox) | Implicit (one row per Message-ID regardless) |
| **Cross-system identity** | Via `person_id` from `core` (Phase 2) | Native (`core.conversations` → `core.people`) |

autoreplies *must* resolve which mailbox to send from — the recipient gate is the mechanism for that. message-monitor only observes, so it can key off Message-ID without needing to pick a sender.

## Recommended shared solution

**Adopt RFC-822 `Message-ID` as the canonical email-dedup key in both systems, with autoreplies layering the recipient-gate mailbox-selection on top.**

### Concrete steps

1. **autoreplies:** Extract and store the RFC-822 `Message-ID` header alongside the Gmail message ID in `replied_fingerprints` and the new `replied_persons` table. Replace the content fingerprint with `(mailbox, rfc822_message_id)` as the Phase-1 dedup key. This is unambiguous (two Hiver copies have the same RFC-822 Message-ID), shrinks the window to zero (exact match), and is robust to minor formatting changes in the StreetEasy/Zillow email body.

2. **message-monitor:** No change needed to its dedup logic. Coordinate to ensure `person_for_contact` RPC is live before autoreplies switches `REPEAT_INQUIRY_MODE=enforce`.

3. **Shared `pear-core` helper (deferred):** A future `pear-core` shared library could expose a `resolve_mailbox_for_message(rfc822_message_id, recipients) -> str` helper that encapsulates the Hiver recipient-gate logic for reuse across systems. This removes the need for each consumer to re-implement it.

### Migration path for autoreplies

1. Add RFC-822 `Message-ID` extraction to the email parsing layer (`parsers/base.py`).
2. Store it in `replied_fingerprints` (new optional column) and `replied_persons`.
3. Dual-write during a transition window: keep the content fingerprint for backward compatibility with existing SQLite state, add the Message-ID check as a second gate.
4. After one full `DEDUP_WINDOW_SECONDS` (1 hour) has elapsed since the cutover deploy, remove the content-fingerprint check.

### Tradeoffs

| | Current content fingerprint | RFC-822 Message-ID |
|---|---|---|
| **Robustness** | Breaks if StreetEasy slightly changes email formatting | Stable as long as the sending MTA assigns Message-IDs |
| **Window** | 1 hour (configurable) | Zero — exact match is instant |
| **Existing state** | Already populated in SQLite | Requires migration or dual-write |
| **Re-notification coverage** | Catches StreetEasy "re-notify" emails (same body, new Gmail message ID, same RFC-822 Message-ID) | Also catches these — same behavior |
| **Cross-mailbox Hiver copies** | Requires recipient gate (already in place) | Requires recipient gate (already in place) |

Neither approach requires changes to the Hiver integration itself.
