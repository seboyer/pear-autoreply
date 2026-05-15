# Pear Autoreplies — Testing Harness Plan

**Status:** draft for approval — *2026-05-06*
**Owner:** Sam
**Companion to:** [`PLAN.md`](./PLAN.md)
**Goal:** validate the rewritten pipeline silently and side-by-side with the legacy Zapier flow before any cutover.

---

## TL;DR

A second run mode of the same codebase that ingests the same live StreetEasy / Zillow lead emails as the production system, runs them through the *exact same* parsing, matching, and reply-generation logic, but writes results into a **testing copy of the Pear Tracker base** instead of Gmail-sending a reply. Every reply that *would* have been sent is materialized as a row in a new `Drafts` table on the test base for human review. Supabase and Slack are short-circuited by default. The legacy Zapier autoreplies keep running untouched, so production behavior is unchanged for the duration of the validation window.

The harness is built as a thin alternate entrypoint and a set of pluggable side-effect strategies, **not** as a fork of the pipeline — every code path the harness exercises is the same one production will use.

---

## Design principles

1. **Reuse, don't fork.** The parser, reply generator, apartment matcher, user matcher, and Airtable inquiry writer are all the same modules production will use. Anything we test in the harness *is* the production code path.
2. **Silent by default.** Zero outbound side effects (no Gmail send, no Slack post, no Supabase write) unless explicitly opted in. Every effect that *would* fire is recorded as a row in the test base.
3. **Audit-grade output.** A reviewer should be able to open the test Airtable base, click any synthetic inquiry, see the linked Draft, and judge — in under 30 seconds — whether the system did the right thing.
4. **Don't cross production wires.** The harness writes only to the test base. It does not call Gmail's `users.send`, does not post to `#platform-leads`, does not write to the prod Supabase project.
5. **Observability over instrumentation.** All decision points the system makes (parser strategy, apartment match confidence, template source, would-be reply route) are persisted on the Drafts row, not just logged.

---

## Decisions (proposed — flag any disagreement)

| Decision | Recommendation | Why |
|---|---|---|
| Ingestion mode | **Query-based polling** every 60s — `messages.list(q="from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com) after:<unix_ts>")` per agent mailbox (not Pub/Sub, not `history.list`/label-based) | Pub/Sub and the Pear/Leads filter+label are production-cutover concerns; the harness sidesteps both. Query-based polling needs no Gmail filter setup, no label-ID resolution per mailbox, and no history-id expiry handling. Sender allowlist is a stable empirical contract (PLAN.md § 1). |
| Where it runs | **Same droplet as production**, as a separate Docker Compose service (`harness-poller`) | Until cutover, prod containers are idle (Zapier is still handling traffic). Adding a sidecar service uses resources nothing else is using; tear-down at cutover is `docker compose down harness-poller`. No extra droplet, no extra secrets store, no extra logs pipeline. |
| Test base | New base, native-synced from prod for **Apartments, Buildings, Management, Users** | Sam already plans this. We only need to add: a fresh **Inquiries** table (writable, identical field set to prod) + a new **Drafts** table. |
| Schema regeneration | `scripts/generate_airtable_schema.py` extended to take a `--base` flag and emit a `TEST` `PearTrackerSchema` alongside `PROD`. Both live in `airtable_schema.py`. | Keeps the immutable-IDs rule intact — never names. The harness selects `PROD` or `TEST` by base ID. |
| Side-effect plumbing | **Strategy pattern**: `SendStrategy`, `SlackStrategy`, `SupabaseStrategy` interfaces. Production wires `LiveSend / LiveSlack / LiveSupabase`. Harness wires `DraftSend / NoopSlack / NoopSupabase`. | Same pipeline code, different leaf calls. Easier to grow than a `mode` flag scattered across the pipeline. |
| Supabase in tests | **Skip by default**, `--write-supabase` flag wires a `TestSupabase` strategy that writes to a fresh `inquiries_test` table (or new project) | The legacy Zapier path still feeds prod Supabase; we don't need test data interleaved there. Including a flag means we *can* validate the new ID semantics (Airtable record ID as PK) without committing to it on day one. |
| Slack in tests | **Skip by default**, optional `--slack-channel #platform-leads-test` to post to a private test channel | Initially we want absolute silence; later, posting to a test channel is a faster review surface than scrolling Airtable. |
| LLM in tests | **Live calls** to Anthropic Haiku 4.5 (no mocks) | The whole point is to validate real reply quality. ~$1–3/day cost is not a concern at this volume. |
| Dedupe state | Local **SQLite** file (`harness/state.sqlite`) tracking last seen Gmail historyId per mailbox + processed message-ids | No Redis dependency for the harness. Keeps the harness self-contained and easy to wipe between runs. |
| Replay & backfill | CLI: `harness watch`, `harness backfill --since YYYY-MM-DD`, `harness replay <message_id>` | Backfill lets us batch-validate against the last week of leads on day one rather than waiting for new leads to dribble in. |
| Diff vs prod | CLI: `harness diff --since YYYY-MM-DD` reads both the test Inquiries table and prod Inquiries table, joins on Gmail message-id where possible, reports per-field deltas as a CSV / Slack post | This is the key validation tool. It quantifies how the new pipeline diverges from Zapier on real production traffic. |
| Lead sources | Same as production (StreetEasy + Zillow) | No reason to differ. |
| Quiet hours / jitter | **Computed and recorded** on the Draft row (`would_send_at`), but not enforced | We want to validate the math without delaying the test loop. |
| Run cadence | One worker process, single-threaded, polls every 60s | Plenty of headroom at 75/day on the legacy mailbox; small enough to debug. |

---

## End-to-end flow

```
StreetEasy/Zillow email
        │
        ▼
Gmail (Workspace, agent@pearnyc.com)  ← labeled "Pear/Leads" by the existing filter
        │
        │   [legacy Zapier] ──► auto-reply + prod Airtable + prod Supabase + prod Slack
        │                       (UNTOUCHED — production stays on Zapier during validation)
        │
        │   [harness poller, every 60s]
        ▼
   gmail.users.messages.list(
       userId=mailbox,
       q="from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com) after:<unix_ts>")
        │
        │   for each new message-id, dedup against SQLite state
        ▼
   Worker pipeline (same modules as production)
        │
        ├── parse_lead_email                 ← production parser
        ├── compose_reply (Haiku 4.5 live)   ← production reply generator
        ├── match_apartment(TEST base)       ← production matcher, pointed at synced TEST apartments
        ├── match_existing_user(TEST base)   ← production matcher, pointed at synced TEST users
        │
        ▼
   AirtableClient(schema=TEST).create_inquiry(...)        ← writes to TEST Inquiries table
        │
        ▼
   AirtableClient(schema=TEST).create_draft(             ← writes to NEW TEST Drafts table
       inquiry_id=...,
       to=resolved_reply_to,
       subject=draft_subject,
       body_plaintext=draft_body,
       body_html=draft_body_html,
       parser_used=..., template_source=...,
       reply_route=..., would_send_at=...,
       apartment_match_strategy=..., apartment_match_confidence=...,
       llm_model=..., llm_latency_ms=...,
       notes=...,
   )
        │
        │  [DraftSend] does NOT call gmail.users.send
        │  [NoopSlack] does NOT post
        │  [NoopSupabase] does NOT write
        ▼
   SQLite: mark message-id processed, advance historyId
```

A reviewer's daily loop is: open Airtable test base → Drafts view sorted by Created descending → spot-check a handful → flag anything the comparison report (`harness diff`) called out as divergent vs prod.

---

## Component breakdown

### 1. Harness ingestion — Gmail query-based polling

A long-running worker (`autoreplies.harness.poller`) iterating per agent mailbox:

```python
LEAD_SENDER_QUERY = (
    "from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com)"
)

for mailbox in agent_mailboxes:
    last_seen_ms = state.get_last_seen(mailbox) or (now_ms() - bootstrap_lookback_ms)
    q = f"{LEAD_SENDER_QUERY} after:{last_seen_ms // 1000}"
    messages = gmail.messages_list(mailbox, q=q)
    max_internal_date = last_seen_ms
    for msg_id, internal_date_ms in messages:
        if not state.was_processed(msg_id):
            run_pipeline(message_id=msg_id, mailbox_email=mailbox, mode="harness")
            state.mark_processed(msg_id)
        max_internal_date = max(max_internal_date, internal_date_ms)
    state.set_last_seen(mailbox, max_internal_date)
```

- **Scopes:** identical to production (`gmail.modify`). The same service account with domain-wide delegation works — no new GCP setup. The harness does not need `gmail.settings.basic` because it never creates filters or reads signatures (signatures are not needed for the draft surface).
- **Discovery of mailboxes:** read `Users` table, filter `Autoreply Enabled (Agent) = TRUE`, project `Autoreply Email (Agent)` (the legacy per-user inbox column — distinct from the primary `Email`). Natively-synced Users from prod give us the right list with no manual maintenance. Rows with the checkbox set but the inbox field blank are skipped with a warning.
- **Bootstrap:** on first run for a mailbox, set `last_seen = now - 60s` (don't backfill — that's a separate subcommand).
- **No history-id expiry handling needed.** `messages.list` is timestamp-based, so the harness can resume cleanly after an arbitrary downtime — it'll just process whatever's accumulated since `last_seen_ms`. There's no 7-day cliff like `history.list` has.
- **Why not the PLAN.md ingestion path?** Production uses `Pear/Leads` filter+label+watch+Pub/Sub for sub-30s time-to-reply at scale. The harness has no latency budget and validates the parser/matcher/LLM/Airtable layer, not the Gmail filter layer. Skipping the filter+label setup means Sam doesn't need to touch each agent's inbox (or run a per-mailbox filter-install script) just to start validation.

### 2. Pipeline — same code as production, swapped strategies

Production today wires its side effects directly inside `process_lead`. We refactor those three call sites to go through small protocols:

```python
class SendStrategy(Protocol):
    def send_reply(self, *, to, subject, plaintext_body, html_body,
                   in_reply_to_message_id, thread_id, agent, parsed) -> SendResult: ...

class SlackStrategy(Protocol):
    def post_lead(self, *, ...) -> str: ...
    def post_alert(self, *, summary, details) -> str: ...

class SupabaseStrategy(Protocol):
    def upsert_inquiry(self, **fields) -> dict: ...
```

Two implementations of each:

| Strategy | Production wires | Harness wires |
|---|---|---|
| Send | `LiveSend` (calls `gmail.send_reply`) | `DraftSend` (writes a Drafts row + computes `would_send_at`) |
| Slack | `LiveSlack` (current `SlackClient.post_lead`) | `NoopSlack` (writes nothing — or `TestChannelSlack` if `--slack-channel` provided) |
| Supabase | `LiveSupabase` (current `SupabaseClient.upsert_inquiry`) | `NoopSupabase` (default) or `TestSupabase` (with `--write-supabase`) |

The pipeline orchestration (`pipeline/process_lead.py`) doesn't change — only the strategies it's constructed with. This is a small refactor that *also* leaves production code cleaner (DI-friendly, easier to unit test).

### 3. Test base schema

**New tables to create on the test base** (Sam — to be done in Airtable UI, then re-run schema generation):

1. **Inquiries** — same schema as prod Inquiries:
   - `Name (Form)`, `Email (Form)`, `Phone`, `Message`
   - `Apartment` (linked to synced Apartments table — editable)
   - `User` (linked to synced Users table — editable)
   - `Method` (single-select: `Web`)
   - `Type (Non Website)` (single-select: `StreetEasy`, `Zillow`)
   - `Gmail Message ID (Autoreply)` (single-line text)
   - `Date Created` (auto)
   - **`Agent`** (lookup, follows `Apartment → Agent` exactly as in prod — depends on the synced Apartment having an Agent linked, which the native sync preserves)

2. **Drafts** — new table (no prod counterpart):
   - `Inquiry` (linked to test Inquiries — many-to-one)
   - `Recipient` (single-line — the resolved Reply-To)
   - `Subject`, `Body Plaintext`, `Body HTML` (long text)
   - `Source` (single-select: `StreetEasy`, `Zillow`)
   - `Parser Used` (single-select: `regex`, `llm_fallback`)
   - `Template Source` (single-select: `agent`, `pear_default`)
   - `Reply Route` (single-select: `thread`, `direct`, `skipped`)
   - `Skipped Reason` (long text — populated only when route=skipped)
   - `Apartment Match Strategy` (single-select: `streeteasy_id`, `address`, `none`)
   - `Apartment Match Confidence` (number 0–100, blank when none)
   - `LLM Model` (single-line)
   - `LLM Latency Ms` (number)
   - `Would Send At` (datetime — after jitter + quiet-hours computed; not enforced)
   - `Notes / Warnings` (long text — multi-line concatenation of any soft warnings)
   - `Gmail Message ID` (single-line — duplicate of the Inquiry's; convenience for reviewers)
   - `Created Time` (auto)

**Schema regeneration:**

```bash
python scripts/generate_airtable_schema.py --base TEST  # adds TEST entry
python scripts/generate_airtable_schema.py --base PROD  # unchanged
```

Both `PROD` and `TEST` end up in the same `airtable_schema.py`; the `SCHEMAS` dict resolves by base ID. The CURATED dict at the top of the script grows a `drafts` section gated by base.

### 4. State store

Local SQLite in the harness package:

```sql
CREATE TABLE mailbox_state (
    mailbox_email TEXT PRIMARY KEY,
    last_seen_internal_date_ms INTEGER NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE processed_messages (
    gmail_message_id TEXT PRIMARY KEY,
    mailbox_email TEXT NOT NULL,
    airtable_inquiry_id TEXT,
    airtable_draft_id TEXT,
    processed_at TIMESTAMP NOT NULL,
    error TEXT
);
```

Single file, no service dependency. Wiped between runs if needed (`make harness-reset`). Production keeps its Redis-backed state per PLAN.md — the two storage layers do not interact.

### 5. CLI surface

A new entrypoint (`python -m autoreplies.harness ...`) wired through Make targets:

```
harness watch
    Long-running poll loop. Default interval 60s. Logs to stdout.

harness backfill --since 2026-04-29 [--mailbox alice@pearnyc.com]
    Process every labeled message in the window. Useful for the first day
    of validation — gives us ~75 leads/day × N days of test data immediately.

harness replay <gmail_message_id>
    Re-run a single message. Skips dedup. Useful for debugging.

harness diff --since 2026-04-29 [--out report.csv]
    Read prod Inquiries (Zapier-fed) and test Inquiries (harness-fed) for the
    window. Match on Gmail Message ID where present (prod legacy rows lack it
    pre-2026-04, so older joins fall back to email + listing-url). Report per-row:
      - present_in_prod / present_in_test / both
      - field deltas: apartment match (yes/no, confidence, target record),
                      user match (yes/no), parsed first_name, phone, message
      - would-have-sent vs Zapier-actually-sent (manual review case)

harness stats --since 2026-04-29
    Aggregate metrics over the window:
      - parser success rate per source vs the empirical baseline
        (StreetEasy: first_name 96%, phone 85%, address 95.5%, listing_url 100%;
         Zillow: address 100%, listing_url 100%)
      - apartment match rate (URL-based vs address-fuzzy vs none)
      - user match rate
      - reply route breakdown (thread / direct / skipped)
      - LLM template-fill failure rate (post-check rejections)
      - p50 / p95 pipeline latency
```

### 6. Operator runbook

```bash
# === ONE-TIME SETUP ===

# 1. Airtable test base (manual, in the UI)
#    a. Duplicate prod base structure (or start fresh)
#    b. Set up native sync from prod for Apartments, Buildings, Management, Users
#    c. Add Inquiries table (mirror prod fields — see § 3)
#    d. Add Drafts table (per § 3 spec)
#    e. Verify: write one test record linking a synced Apartment record by prod ID;
#       confirm the link resolves correctly in the test base. (Open item #7.)

# 2. Regenerate the schema module to include TEST
make schema-regen ARGS="--base TEST"

# 3. Provision the harness on the prod droplet
#    a. Pull latest code on the droplet
#    b. Drop a `.env.harness` next to the existing `.env` with:
#         AIRTABLE_TEST_BASE_ID=app...
#         HARNESS_POLL_INTERVAL_SECONDS=60
#         HARNESS_BOOTSTRAP_LOOKBACK_SECONDS=60
#         HARNESS_STATE_PATH=/var/lib/pear-autoreply/harness.sqlite
#         (no SLACK_BOT_TOKEN, no SUPABASE_URL needed)
#         (no label ID — polling is query-based, not label-based)
#    c. `docker compose up -d harness-poller`

# === DAY-ONE BACKFILL ===

# 4. Seed the test base with 7 days of historical leads
docker compose run --rm harness-poller python -m autoreplies.harness backfill --since 2026-04-29

# 5. Sam + sales team review the resulting Drafts in Airtable.
#    If parser, apartment match, and template fill look right, proceed.
#    If not: file fixes, re-run backfill against a clean test base.

# === STEADY-STATE VALIDATION ===

# 6. The harness-poller container is already running (step 3c).
#    It polls every 60s, dedups against SQLite, writes Drafts as new leads land.

# 7. Daily review cadence — open the test base, scan recent Drafts.
#    Optional reports for spot metrics:
docker compose run --rm harness-poller python -m autoreplies.harness diff   --since 2026-04-29 --out /tmp/diff-2026-05-06.csv
docker compose run --rm harness-poller python -m autoreplies.harness stats  --since 2026-04-29

# === CUTOVER ===

# 8. When the team agrees the system is ready:
#    a. Stop the harness:                 docker compose stop harness-poller
#    b. Follow PLAN.md § "Implementation phases" Phase 5 cutover
#    c. (Optional) keep harness-poller running for a tail period for safety —
#       it does not interfere with prod since it only reads Gmail and writes
#       to the test base.
#    d. Once you're confident, remove it:  docker compose rm harness-poller
```

The harness service is defined in `docker-compose.yml` alongside `web`/`worker`/`scheduler`/`redis`. It uses the same image as `worker`, just a different command (`python -m autoreplies.harness watch`) and a different env file (`.env.harness`). It does not depend on Redis (uses local SQLite at `/var/lib/pear-autoreply/harness.sqlite`, mounted as a Docker volume so it survives container restarts).

---

## Failure modes & idempotency

| Scenario | Behavior |
|---|---|
| Polling sees the same message-id twice | SQLite `processed_messages` short-circuits |
| Pipeline crashes mid-run for a message | The next poll re-includes it; SQLite is updated only on success |
| Test Inquiries insert succeeds, Drafts insert fails | Retry creates a duplicate inquiry — to avoid, we either (a) write both in a single Airtable batch, or (b) look up the existing inquiry by Gmail message-id before inserting. **Recommendation: (b)** — same idempotency pattern as production (`find_inquiry_by_gmail_message_id`). |
| Harness downtime | `messages.list` with `after:<last_seen_unix_ts>` resumes cleanly on next start — no history-id 7-day cliff to worry about |
| Anthropic 429 / 5xx | Same retry logic as prod; if final fallback to literal-fill, mark Draft `parser_used = template_only` and continue |
| Airtable rate-limit hit on test base | Same token-bucket as prod (5 req/sec); harness pace is well below this |
| Agent not in TEST Users (sync gap) | Skip with a Notes entry on a special "orphans" sentinel Inquiry, OR write a Draft with `Skipped Reason = "agent not synced"` and no Inquiry. **Recommendation: write the Draft with skipped reason; no Inquiry row.** |
| Test base schema drifts | Schema generation script is the source of truth — re-run it after any test-base edit. CI lint can flag if the checked-in schema is older than the most recent metadata pull. |

---

## What the harness does NOT do (deliberate)

- **Does not touch production data.** No writes to prod Airtable, prod Supabase, or `#platform-leads`.
- **Does not Gmail-send anything.** Every reply lives only as a Draft row.
- **Does not race the legacy Zapier flow.** Polling is read-only; Zapier keeps handling production traffic for the validation window.
- **Does not validate the Pub/Sub plumbing.** That's a single integration test at cutover, not a soak.
- **Does not re-process historical leads from before the harness started, unless `backfill` is invoked.**
- **Does not test the watch-renewal cron.** Production-only concern; out of scope here.

---

## Decisions confirmed by Sam (2026-05-06)

| # | Item | Resolution |
|---|---|---|
| 1 | Validation criteria | **Human review.** Sam + sales team review Drafts in Airtable; harness runs until they agree it's ready, then we cut over. No automated pass/fail thresholds. |
| 2 | Slack during validation | **Silent.** No Slack output from the harness. (`NoopSlack` strategy, no Slack env vars required.) |
| 3 | Supabase scope | **Skipped by default**, with an opt-in `--write-supabase` flag wiring a `TestSupabase` strategy that targets a `inquiries_test` table in the existing Supabase project. Not enabled in normal harness operation. |
| 4 | Where it runs | **Same droplet as production**, separate Docker Compose service. Not on Sam's laptop — must run unattended for the full validation window. |
| 5 | Mailbox scope | **All agent mailboxes from day one.** No canary phase. |

## Remaining open items (defaults applied unless Sam pushes back)

| # | Item | Default |
|---|---|---|
| 6 | Backfill window | **7 days.** Populates the test base immediately for human review with ~75/day × N agents × 7 days of real leads. |
| 7 | Native-sync linked-record semantics | **Confirmed:** prod IDs and test IDs differ. No translation needed at harness runtime (matchers query the test base directly). A `RecordIdTranslator` is needed only in the diff tool — built by reading a synced "Source Record ID" column on each synced table. Prerequisite: add a `RECORD_ID()` formula field to prod Apartments, Users, Buildings, Management and re-sync. |
| 8 | TEST base ID | `appmSm1FyerysvtcX` |

---

## Appendix A — Code-level changes summary

What the build phase will need to touch:

```
src/autoreplies/
  pipeline/
    process_lead.py        # refactor: accept SendStrategy / SlackStrategy / SupabaseStrategy
    strategies.py          # NEW: protocols + Live* + Noop*/Draft* implementations
  services/
    airtable.py            # add: create_draft(...) method + Drafts dataclass on schema
    airtable_schema.py     # regenerate: add TEST PearTrackerSchema + DraftsTable
  harness/                 # NEW package
    __init__.py
    poller.py              # Gmail history polling loop
    state.py               # SQLite state store
    runner.py              # CLI dispatch (watch / backfill / replay / diff / stats)
    diff.py                # cross-base comparison report
    stats.py               # aggregate metrics report
  config.py                # add: airtable_test_base_id, harness_poll_interval_seconds,
                           #      harness_bootstrap_lookback_seconds, harness_state_path

scripts/
  generate_airtable_schema.py    # add --base TEST/PROD flag, extend CURATED with Drafts

tests/
  harness/                       # NEW: unit tests for poller, state, strategies, diff
  pipeline/                      # extend: parameterize over SendStrategy

Makefile                         # add: harness-watch, harness-backfill, harness-replay,
                                 #      harness-diff, harness-stats, schema-regen-test

.env.example                     # add: AIRTABLE_TEST_BASE_ID, HARNESS_POLL_INTERVAL_SECONDS,
                                 #      HARNESS_BOOTSTRAP_LOOKBACK_SECONDS, HARNESS_STATE_PATH
```

No production-mode behavior changes other than the strategy-pattern refactor (which is a wiring change with no functional difference in `LiveSend` / `LiveSlack` / `LiveSupabase`).

---

## Appendix B — Why polling, not Pub/Sub, for the harness

Pub/Sub push requires a publicly-reachable HTTPS endpoint with a verified TLS cert. For a *production* lead pipeline that needs sub-30-second time-to-reply, that's worth setting up. For a harness whose job is offline validation, the engineering cost is unjustified:

- **No latency requirement.** A 60-second mean delay between a lead arriving and the harness recording its Draft is fine — nobody is waiting on this loop.
- **No deploy pressure.** Polling runs locally, on a droplet, or in a tmux window; Pub/Sub forces production-shaped infrastructure (subscriptions, audience IDs, JWT verification, public hostname).
- **No coordination with the prod Pub/Sub topic.** If we wanted Pub/Sub, we'd have to either share the topic (risk: harness ack fails get retried into prod) or duplicate the topic (more setup). Polling sidesteps both.
- **One-time integration test for the Pub/Sub path is cheap.** When we cut over to prod, we test the webhook end-to-end with one canary email; the rest of the system the harness has already validated.

---

## Appendix C — Why a separate Drafts table, not extra columns on Inquiries

Two reasons:

1. **Schema parity for the diff tool.** Test Inquiries needs to be a column-by-column mirror of prod Inquiries so `harness diff` can do a clean field-vs-field comparison. Stuffing harness-only diagnostic columns onto Inquiries breaks that parity and makes the diff noisier.
2. **Drafts have a different unit of analysis.** Inquiries are about the *lead*; Drafts are about the *would-have-sent reply*. They're 1:1 today, but conceptually a single inquiry could produce multiple draft attempts (e.g. an LLM-generated draft *and* a literal-fill fallback). Keeping them separate now leaves room for that without a schema migration.
