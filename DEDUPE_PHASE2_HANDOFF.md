# Handoff: Phase 2 dedupe — repeated inquiries from the same person

> **For a fresh session, picked up later.** Self-contained — assumes the repo +
> CLAUDE.md, not the conversation that produced it. Read CLAUDE.md and PLAN.md first,
> then this. Phase 1 (duplicate-message suppression) is **done**; this is Phase 2.
>
> **This doc is now part design record, part as-built reference.** The original design
> below was written before implementation and **the shipped behavior diverges from it in
> three material ways** (see "As built" → *Where the shipped design diverges*). Read the
> as-built section as authoritative; the rest is retained for rationale.

## Status: IMPLEMENTED and validated — cleared to enforce (2026-08-02)

Shipped in **PR #28**, on `main`. The original blocker (message-monitor go-live +
client people-sync) is **resolved**: `public.person_for_contact` exists and resolves
~89–93% of inbound leads.

Rollout is a three-state flag, `REPEAT_INQUIRY_MODE` ∈ `off` | `observe` | `enforce`
(`config.py:97`, default `off`), with `REPEAT_INQUIRY_WINDOW_SECONDS` default
`1209600` (14 days, `config.py:92`).

- `off` — Phase 2 is fully inert; the identity RPC is never called.
- `observe` — resolves identity, logs `_phase_a: OBSERVE repeat inquiry …`, and
  **still records `replied_persons`**. Sends are unchanged.
- `enforce` — same, but swaps the template.

**Observe ran 2026-06-11 → 07-28** (12,942 leads, ~275/day) and validated cleanly:
identity resolution ~89–93%, zero over-merge, **20.1% of leads flagged as repeats
≈ 43/day**. Sam cleared the flip to `enforce` on 2026-08-02.

Because `record_person_reply` runs in `observe` as well (gated only on
`person_id is not None`, `process_lead.py:486`), `replied_persons` is already warm —
flipping to `enforce` has **no cold-start blind spot**.

## As built (authoritative — read this before the design sections)

**Dedup key: `(person_id, mailbox_email)` within 14 days.** Not the apartment, and not
an "active conversation" join. A returning prospect who contacts the *same agent
mailbox* inside the window is a repeat, whether or not it's the same listing.

**Behavior on a repeat: swap the template — do not suppress.** The inquiry is still
recorded in Airtable, Slack still fires, and a reply is still sent. The only change is
which template renders: `Users.Autoreply Repeat Template (Agent)`
(`fldZP0fx15Yp4IRof`), falling back to `FALLBACK_REPEAT_TEMPLATE.md` when that field is
blank. `template_source` records `"agent"` or `"pear_default"` accordingly.

| Piece | Where |
|---|---|
| Mode gate + identity resolve + repeat lookup | `pipeline/process_lead.py` step **2c** (right after the Phase-1 fingerprint dedup) |
| Template selection | `pipeline/process_lead.py` step **9** — `get_repeat_template_for_agent` when `is_repeat`, else `get_template_for_agent` |
| Recording the reply into the window | `pipeline/process_lead.py` step **11c** — runs for first-touch **and** repeat sends |
| Identity RPC | `services/supabase.py::resolve_person_id` → `POST /rest/v1/rpc/person_for_contact` |
| Window state | `workers/poller_state.py` → `replied_persons` (person_id, mailbox_email, gmail_message_id, replied_at) |
| Template resolution + rich-text un-escaping | `services/templates.py::get_repeat_template_for_agent`, `_unescape_rich_text` |
| Tests for the enforce branch | `tests/pipeline/test_process_lead.py:591-771` |

**Fail-open at both steps.** A raised exception from `resolve_person_id` or
`recent_person_reply` is logged and treated as "not a repeat", so the prospect gets the
normal first-touch reply. The revenue path never depends on the identity layer being up.

### Where the shipped design diverges from the design sections below

1. **Suppress → swap.** The design recommended *suppressing the auto-send* (route =
   skipped) on a repeat. What shipped instead **always sends**, using shorter
   repeat-specific copy. Rationale: a returning prospect should still get an
   acknowledgment; the thing to avoid is re-firing the full intake questionnaire into a
   live conversation, not going silent. Anything below describing `route = skipped` or a
   `should_auto_reply(...) -> Decision` shape is **not** what exists.
2. **"Active conversation" → mailbox + time window.** The design keyed on *(same
   person) AND (active conversation)* via a `core.conversations` join. What shipped keys
   on *(same person) AND (same agent mailbox) AND (within 14 days)*. There is no
   `core.conversations` read.
3. **Listing is deliberately not in the key.** The design floated "same person, genuinely
   new listing after a gap → may still warrant a first-touch." Sam **explicitly rejected**
   adding apartment to the key (2026-08-02): the goal is that a client with an ongoing
   thread never receives another first-touch email, same listing or not. The repeat
   template copy is worded to read correctly either way ("your additional inquiry for
   {{apartment_address|this listing}}"). Observe data showed the split was ~50/50
   same-vs-different listing, so this is a real behavioral choice, not a no-op.

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
> ⚠️ **Superseded.** This is the pre-implementation proposal. What shipped swaps the
> template rather than suppressing the send, and keys on mailbox + 14-day window rather
> than an "active conversation" — see *As built* above. Retained for rationale only.

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
check after those (it needs the parsed lead + identity).
*(As built: this seam was used as designed — the gate is step 2c. The
`should_auto_reply(...) -> Decision` shape was not; template selection happens later, at
step 9, and no send is ever skipped.)*

## The access-path wrinkle — RESOLVED: option (b)

autoreplies reaches Supabase via **PostgREST HTTP against the `public` schema only**
(`services/supabase.py` — `httpx` to `/rest/v1/...` with the service-role key). Reading
`core` is therefore **not just a grant**: PostgREST exposes `public` by default.
Options:
- **(a)** Add `core` to the project's PostgREST exposed-schemas setting **+** grant the
  autoreplies role SELECT on `core.*`. Project-wide change (message-monitor's project).
- **(b) ✅ CHOSEN and shipped:** message-monitor exposes a **`public` RPC** that
  autoreplies calls via PostgREST with no schema exposure — an explicit, owned, minimal
  contract boundary. The as-built function is **`public.person_for_contact(p_email,
  p_phone)`** (the design's `person_recent_contact` name was not used). Caller:
  `services/supabase.py::resolve_person_id`. autoreplies never reads `core` directly.

## Safety / rollout

- **Fail open.** Never gate the revenue path on the observer. If `core` is unreachable
  or returns nothing, default to current behavior (auto-reply). autoreplies is the
  revenue path; message-monitor is an observer.
- **Observe-then-enforce.** ✅ Done as designed — `REPEAT_INQUIRY_MODE` shipped `off`,
  ran 47 days in `observe`, cleared for `enforce` 2026-08-02.
- **CLAUDE.md hard rules still apply:** never create a User from a lead; Airtable by
  immutable ID; all Airtable formulas via `pyairtable.formulas`.

## Operating notes (post-launch)

- **Verify a repeat send by its rendered body, not by `template_source`.** All 7
  autoreply-enabled agents currently have their repeat field *populated* with text
  identical to `FALLBACK_REPEAT_TEMPLATE.md` — done so agents have something to edit
  from, not for a technical reason. Consequence: repeat rows record
  `template_source = "agent"`, **not** `"pear_default"`, and the fallback file is
  effectively unreachable in production until an agent clears their field or an 8th
  agent is enabled.
- **Both template fields are Airtable `richText`**, so values arrive Markdown-escaped
  (`{{first\_name|there}}`). `_unescape_rich_text` (PR #21) reverses this before slot
  parsing. A repeat template authored outside Airtable must survive that round-trip.
- **Deploys are decoupled from the flag.** Render `autoDeploy` is off; changing
  `REPEAT_INQUIRY_MODE` restarts the service against the *same image* — no rebuild, and
  no need to ship unrelated commits sitting on `main` just to flip the mode.
- **Agents' repeat templates may diverge over time.** Identical-to-fallback is the
  starting state, not an invariant — don't write checks that assume the values match.

## Deliverable — ✅ complete

The design proposal was reviewed and implemented in **PR #28**. This document is now the
as-built record.

## Pointers

| What | Where |
|---|---|
| **Phase 2 implementation** | **PR #28**; `pipeline/process_lead.py` (2c gate, 9 template swap, 11c record), `workers/poller_state.py` (`replied_persons`), `services/supabase.py` (`resolve_person_id`), `services/templates.py` (`get_repeat_template_for_agent`) |
| Phase 1 (the dispatch-layer seam Phase 2 reuses) | PR #27; `pipeline/process_lead.py` (1a Hiver gate, 2b fingerprint dedup), `pipeline/dedup.py` |
| message-monitor repo | `~/Dev/message-monitor` — `CLAUDE.md`, `migrations/0001_core.sql` (identity schema), `todo.md` (people-sync status + `sync_people` merge bug) |
| message-monitor design/roadmap | `~/.claude/plans/happy-growing-manatee.md` |
| `core` schema (from `0001_core.sql`) | `core.people` (id uuid, `airtable_user_id` nullable UNIQUE, `display_name`, `role`∈{agent,admin,client,landlord_owner,unknown}); `core.person_identifiers` (`person_id`, `kind`∈{email,phone}, `value` UNIQUE); `core.conversations` (`person_id`, `inquiry_id` soft-ref to `public.inquiries.id`, `gmail_thread_id`, `channel_hint`) |
| autoreplies Supabase transport (the wrinkle) | `services/supabase.py` (PostgREST/`public` only) |
| autoreplies user match (never-create rule) | `services/airtable.py` → `find_existing_user` |
| Original 2-phase brief / Phase-1 record | `DEDUPE_HANDOFF.md` |
| Deferred note | PLAN.md "Remaining open items" |
