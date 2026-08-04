# Handoff: StreetEasy/Zillow repeated-inquiry & duplicate-message dedupe (2 phases)

> **For a fresh session.** This is a self-contained brief — it assumes the repo +
> CLAUDE.md, not the conversation that produced it. Read CLAUDE.md and PLAN.md first.

This task adds **dedupe** so a prospect doesn't receive the same auto-reply more than
once. It was deliberately deferred during launch; it's logged in PLAN.md "Remaining
open items" (entry "Repeated / duplicate inquiries").

> **STATUS (2026-08-02):**
> - **Phase 1 (duplicate messages) — ✅ DONE, PR #27.** Content+sender fingerprint
>   dedupe at the dispatch layer.
> - **Phase 2 (repeated inquiries from the same person) — ✅ DONE, PR #28**, observe-
>   validated and cleared for `enforce`. The as-built record is
>   **[DEDUPE_PHASE2_HANDOFF.md](./DEDUPE_PHASE2_HANDOFF.md)** — start there. The
>   Phase 2 section below is the original sketch, superseded by that doc.
>   **Note:** what shipped *swaps the template* on a repeat; it does **not** suppress
>   the send. Any "suppress"/"skip" language below is the superseded proposal.

## Why this exists / current behavior

The pipeline dedupes strictly **per Gmail message-id**:

- The poller's SQLite store (`src/autoreplies/workers/poller_state.py` →
  `PollerState.was_processed`/`mark_processed`, table `processed_messages` keyed on
  `gmail_message_id`) is the only live dedup. `poll_once` in
  `src/autoreplies/workers/poller.py` checks it before dispatching.
- `process_lead`'s Redis `JobState` idempotency is currently a STUB
  (`_load_state` returns a fresh state, `_save_state` is `pass` — Phase-4 TODO in
  `src/autoreplies/pipeline/process_lead.py`), so it never short-circuits.
- `find_or_create_inquiry` (`src/autoreplies/services/airtable.py`) also dedupes only
  on `gmail_message_id` (`find_inquiry_by_gmail_message_id`).

Two distinct gaps result, hence two phases:

1. **Duplicate messages.** StreetEasy frequently RE-SENDS the same inquiry as a second
   email — different `gmail_message_id`, near-identical body, ~minutes apart, grouped
   into the SAME Gmail thread. Because the message-ids differ, both are processed and
   **the prospect gets the same reply twice.**
   Observed real examples (mailbox `garland@pearnyc.com`; may have aged out — find
   fresh ones by querying any monitored mailbox):
     - "Omer Mirzo": msgs `19eb71f50f92be7a` (14:39) + `19eb7261bedb01b6` (14:46),
       thread `19eb71f50f92be7a`, IDENTICAL bodies.
     - "Jose Godinez": msgs `19eb72e0f73fdcb0` + `19eb72fbb3e8729d`, one thread,
       identical "524 Lafayette Avenue #2" inquiry.

2. **Repeated inquiries from the same person.** A prospect may contact multiple times —
   genuine conversation follow-ups (which now arrive as "New Message From <name>" and
   PARSE as leads after PR #20, see below), or new inquiries about different listings.
   Suppressing these correctly is an identity problem, not a content-equality one.

Recently merged, relevant context:

- **PR #20** (`66aba4c`) made the StreetEasy parser accept EVERY prospect-originated
  email regardless of subject (gates on an external prospect `Reply-To` + a
  `mailto:<prospect>` body link, not the subject). This means "New Message From <name>"
  emails — including a prospect's mid-conversation follow-ups — now parse as leads,
  which AMPLIFIES the repeated-inquiry problem. Don't undo this.
- **PR #21** un-escaped Airtable rich-text in agent templates (unrelated, just context).

## Phase 1 — suppress duplicate messages (implement) — ✅ DONE (PR #27)

> Implemented: content+sender fingerprint dedupe in `process_lead` (after parse,
> before any side effect), with a `replied_fingerprints` store on PollerState +
> HarnessState (`pipeline/dedup.py`). Window = `DEDUP_WINDOW_SECONDS` (default 3600s),
> validated against real `garland@` data. The brief below is kept for the record.

Goal: when the SAME inquiry is delivered more than once, auto-reply exactly once.
Genuine new inquiries and genuine follow-ups (different content) must still go through.

Design guidance (decide the specifics yourself; validate against real data):

- Implement at the **dispatch layer, NOT the parser.** The parser must stay a pure
  email→`ParsedLead` function. Likely spots: `poll_once`/dispatch in `poller.py`, or
  early in `process_lead` before `strategies.send.send_reply`.
- The cleanest duplicate signal is **(prospect email / Reply-To) + content equality**
  (normalized body or a content hash). A true re-send has identical body; a follow-up
  does not — so content-equality distinguishes "duplicate" from "follow-up" cleanly.
  Gmail thread-id groups duplicates too, but ALSO groups follow-ups, so thread-id alone
  is insufficient. Consider a short time window.
- Timing matters: replies are humanization-delayed 1–5 min (`compute_send_at` in
  `strategies.py`), so duplicates often both arrive/enqueue BEFORE the first reply is
  sent. Dedupe at processing/enqueue time; don't rely on "already sent."
- Storage: extend `PollerState` (add a content/thread dedupe key) or query Airtable
  Inquiries for a recent matching prospect+content. Mind the **sticky-on-failure**
  gotcha (CLAUDE.md): `was_processed` returns true for error rows.
- **Distinctness invariant** (CLAUDE.md): production must never import the harness. The
  harness has its own poller/state; apply the same logic there (or in shared non-harness
  code) without a cross-dependency. The harness only writes Drafts (no send), so it's
  secondary, but keep parity.

Deliverables: the change + tests (mirror the fixture / raw-email style in
`tests/parsers/test_streeteasy.py`), `make lint` / `make typecheck` / `make test` green,
and a validation against real duplicate emails (pattern: a read-only script using
`GmailClient` + `secrets/sa.json`, like `scripts/check_prod_inbox_leads.py`). Open a PR
to `main` (services deploy from `main`; `autoDeploy: false`, so note a
`pear-autoreplies-poller` redeploy is needed — parse/template/dispatch all run in the
poller, not the worker).

## Phase 2 — propose handling for repeated inquiries from the same user (design)

> **➡️ Superseded by [DEDUPE_PHASE2_HANDOFF.md](./DEDUPE_PHASE2_HANDOFF.md).** Phase 2
> is BLOCKED on message-monitor go-live and will be picked up in a future session; that
> doc is the current self-contained brief (it folds in this session's findings: the
> PostgREST `core`-access wrinkle, the deployment blocker, and the "launch in parallel
> with message-monitor" decision). The text below is the original sketch.

Goal: a policy + mechanism for recognizing the same prospect ACROSS inquiries and
deciding when to (not) auto-reply (e.g. don't re-send the full first-touch template into
an active conversation; a genuinely new listing inquiry may still warrant one).

**Before designing:** there is a SEPARATE repo, **`message-monitor`**, at
**`~/Dev/message-monitor`** (alongside this repo), which has **proposed schema changes
for a unified client-identity model.** Start with its `CLAUDE.md`, `migrations/`, and
`todo.md` to find the proposal. Read those proposals first and base your Phase-2 design
on integrating with that model, so client identity is shared/consistent across systems
rather than reinvented here.

Constraints to respect (CLAUDE.md hard rules):

- "Never create a user from a lead." Identity matching uses existing non-staff Users by
  email/phone (`find_existing_user`); on miss, leave the link empty.
- Airtable resources by immutable ID only; all formulas via `pyairtable.formulas`.

Deliverable: a written proposal (design doc / PR description) — the data model
(leveraging message-monitor's unified identity), the reply-policy rules, where it hooks
into the pipeline, and migration/rollout. Implementation can be a follow-up; get the
design reviewed by Sam first.

## Pointers

| What | Where |
|---|---|
| Parser (gates on prospect Reply-To since #20) | `src/autoreplies/parsers/streeteasy.py` |
| Poller + per-message dedup state | `src/autoreplies/workers/poller.py`, `poller_state.py` |
| Pipeline / dispatch / send strategy | `src/autoreplies/pipeline/process_lead.py`, `strategies.py` |
| Airtable client (`find_or_create_inquiry`, `find_existing_user`) | `src/autoreplies/services/airtable.py` |
| Harness (distinctness invariant) | `src/autoreplies/harness/` |
| Deferred note | PLAN.md "Remaining open items" |
| Real-email validation pattern | `scripts/check_prod_inbox_leads.py` |
