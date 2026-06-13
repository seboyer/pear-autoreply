# Handoff: Phase 2 dedupe — repeated inquiries from the same person (design)

> **For a fresh session, picked up later.** Self-contained — assumes the repo +
> CLAUDE.md, not the conversation that produced it. Read CLAUDE.md and PLAN.md first,
> then this. Phase 1 (duplicate-message suppression) is **done**; this is Phase 2.

## Status: BLOCKED on message-monitor go-live

**Do not start implementing until `message-monitor` is deployed AND its client
people-sync has run.** Until then this design delivers ~zero suppression (the shared
identity tables are empty/sparse for prospects), so there's nothing to validate.

- **The blocker:** Phase 2 recognizes the same prospect *across* inquiries using
  `message-monitor`'s shared client-identity model (`core.people` /
  `core.person_identifiers` / `core.conversations`). As of this writing
  `message-monitor` (`~/Dev/message-monitor`) is **not yet deployed**, and only
  *agents* are synced into `core.people` — clients become "holding-pen" rows on first
  inbound, and there's an open `sync_people` merge bug (see its `todo.md`). So prospect
  identity won't be reliably present until it's live and a **full people-sync (~9,024
  users)** has run.
- **Sam's decision (why we wait rather than build a stopgap):** Phase 2 is designed
  **core-integrated** and launched **in parallel with message-monitor**, so the shared
  identity architecture is there to use. **No throwaway interim bridge** (e.g. querying
  autoreplies' own `public.inquiries` by email/phone) — that would reinvent identity
  and get thrown away.
- **How to know it's unblocked:** message-monitor deployed (check its `render.yaml` /
  Render project) and the full Airtable→`core.people` sync run for all users (not just
  agents). Confirm with Sam.

## Why Phase 2 exists (what Phase 1 deliberately leaves on the table)

Phase 1 suppresses **duplicate messages** — the same inquiry re-sent within a short
window (`DEDUP_WINDOW_SECONDS`, default 3600s / 1h). It is intentionally short so it
does **not** touch **repeated inquiries from the same person** that arrive later. Real
data from `garland@` (via `scripts/check_duplicate_inquiries.py`, 7-day sample) shows
the two populations are cleanly separated:

- True re-sends: **≤ ~16 min** apart → Phase 1 suppresses these.
- Genuine re-inquiries (same person, often same listing): **≥ ~16 h** apart → Phase 1
  lets each through, so today each gets a fresh first-touch auto-reply. Examples seen:
  "David Espinosa" re-inquired about 1710 Palmetto #1R **2.7 days** later; "aida" **2.8
  days** later.

Phase 2's job: recognize those as the same prospect and decide, by policy, whether to
re-send the full first-touch template — especially for **mid-conversation follow-ups**,
which now parse as leads after **PR #20** (the StreetEasy parser accepts every
prospect-originated email, including "New Message From <name>"). That PR amplifies the
problem; **don't undo it.** (Separately, **PR #25**'s Hiver recipient-gate already
handles cross-*mailbox* shared-inbox mirror duplicates — a different, deterministic
mechanism; not Phase 2's concern.)

## Design target

**Identity spine — read `message-monitor`'s `core`, never write it.**
- Resolve the inbound prospect (email/phone from `ParsedLead`) → a `person_id` via
  `core.person_identifiers` (UNIQUE on `value`, unifies a person across email/phone and
  channels).
- **Synergy worth leaning on:** `core.people` *holding-pen* rows are message-monitor's
  own identity rows, **not Airtable Users** — so using them does NOT violate the
  CLAUDE.md "never create a User from a lead" rule. `core.people` is exactly the home
  for prospect identity that that rule deliberately leaves homeless.
- `message-monitor` **owns** the `core`/`monitor` schemas and the stability fence is
  one-directional (monitor is SELECT-only on `public.inquiries`; never writes it).
  autoreplies adding **read** access to `core` is a NEW coupling — coordinate it with
  message-monitor (it owns the grants). autoreplies must **read** `core` only, never
  write it.

**Prior-contact signal.** Join `person_id` → `core.conversations`
(`conversations.inquiry_id` is a soft text ref to `public.inquiries.id`; also
`gmail_thread_id`, `channel_hint`) and/or autoreplies' own `public.inquiries`, scoped to
a recency window, to determine whether there's an *active* conversation.

**Reply policy (the product call — get Sam's sign-off before building).**
- New prospect, no prior contact → auto-reply (unchanged).
- Same person, **active conversation** (recent first-touch already sent, or a
  mid-conversation follow-up) → **do not re-fire the first-touch template.** Recommended:
  still record the inquiry + Slack-notify the agent to take over, but **suppress the
  auto-send** (route = skipped, with a reason). The robotic re-greeting into a live human
  conversation is the thing to avoid.
- Same person, **genuinely new listing** after a gap → may still warrant a first-touch.
- Keys on **(same person) AND (active conversation)** — not "same person ever."

**Hook point.** Reuse Phase 1's seam in `pipeline/process_lead._phase_a_create_airtable`
— the post-parse / pre-side-effect region. That region already runs, in order: (1a) the
#25 Hiver recipient-gate, (2) parse, (2b) the Phase-1 fingerprint dedup. Phase 2 adds a
`should_auto_reply(person, parsed, history) -> Decision` check after those (it needs the
parsed lead + identity). Unlike Phase 1 (which fully drops the duplicate), a Phase-2
"suppress" still records the inquiry + notifies Slack — it only skips the auto-send.

## The access-path wrinkle (concrete — decide this early)

autoreplies reaches Supabase via **PostgREST HTTP against the `public` schema only**
(`services/supabase.py` — `httpx` to `/rest/v1/...` with the service-role key). Reading
`core` is therefore **not just a grant**: PostgREST exposes `public` by default.
Options:
- **(a)** Add `core` to the project's PostgREST exposed-schemas setting **+** grant the
  autoreplies role SELECT on `core.*`. Project-wide change (message-monitor's project).
- **(b) Recommended:** have message-monitor expose a **`public` view or RPC** (e.g.
  `public.person_recent_contact(email, phone)`) that autoreplies calls via PostgREST
  with no schema exposure — an explicit, owned, minimal contract boundary. Coordinate
  via a message-monitor migration.

## Safety / rollout

- **Fail open.** Never gate the revenue path on the observer. If `core` is unreachable
  or returns nothing, default to current behavior (auto-reply). autoreplies is the
  revenue path; message-monitor is an observer.
- **Observe-then-enforce.** Ship behind a flag in "observe" mode first (log what *would*
  be suppressed), validate against real repeated-inquiry data, then enforce.
- **CLAUDE.md hard rules still apply:** never create a User from a lead; Airtable by
  immutable ID; all Airtable formulas via `pyairtable.formulas`.

## Deliverable

A written design proposal (data model leveraging `core` identity, reply-policy rules,
hook point, the access-path decision, migration/rollout), **reviewed by Sam before
implementation.** Implementation is a follow-up.

## Pointers

| What | Where |
|---|---|
| Phase 1 (the dispatch-layer seam Phase 2 reuses) | PR #27; `pipeline/process_lead.py` (1a Hiver gate, 2b fingerprint dedup), `pipeline/dedup.py` |
| message-monitor repo | `~/Dev/message-monitor` — `CLAUDE.md`, `migrations/0001_core.sql` (identity schema), `todo.md` (people-sync status + `sync_people` merge bug) |
| message-monitor design/roadmap | `~/.claude/plans/happy-growing-manatee.md` |
| `core` schema (from `0001_core.sql`) | `core.people` (id uuid, `airtable_user_id` nullable UNIQUE, `display_name`, `role`∈{agent,admin,client,landlord_owner,unknown}); `core.person_identifiers` (`person_id`, `kind`∈{email,phone}, `value` UNIQUE); `core.conversations` (`person_id`, `inquiry_id` soft-ref to `public.inquiries.id`, `gmail_thread_id`, `channel_hint`) |
| autoreplies Supabase transport (the wrinkle) | `services/supabase.py` (PostgREST/`public` only) |
| autoreplies user match (never-create rule) | `services/airtable.py` → `find_existing_user` |
| Original 2-phase brief / Phase-1 record | `DEDUPE_HANDOFF.md` |
| Deferred note | PLAN.md "Remaining open items" |
