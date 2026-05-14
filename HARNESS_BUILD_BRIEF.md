# Harness build brief — handoff to Sonnet

**Repository:** `~/Pear/Dev/Autoreplies`
**Stage:** the production scaffolding (Phase 0) is in place; the parsers, services, and pipeline are stubs raising `NotImplementedError`. Your job is to build the **testing harness** end-to-end: a sidecar mode of the same codebase that ingests live StreetEasy/Zillow leads, runs them through the production parsing/matching/reply-generation logic, and writes results to a testing Airtable base **without** sending Gmail replies, posting to Slack, or writing to Supabase.

---

## Read these first, in order

1. [`PLAN.md`](./PLAN.md) — the production system you are building the test side-car for. Source of truth for senders, parser specs, schema rules, idempotency, threading, signature handling, etc. Don't restate or relitigate decisions in here.
2. [`TESTING_HARNESS_PLAN.md`](./TESTING_HARNESS_PLAN.md) — the architecture for what you're building. Reflect the plan; don't deviate from it without surfacing a question first.
3. The current state of `src/autoreplies/` — particularly `services/airtable.py`, `services/airtable_schema.py`, `pipeline/process_lead.py`, `parsers/`, `services/gmail.py`, `services/llm.py`. The stubs already define the right method signatures; you'll fill them in for the harness paths and leave production paths as `NotImplementedError` unless explicitly required.

---

## Hard constraints (do not violate)

- **Airtable resources by immutable ID only.** Never reference tables or fields by display name. New IDs go through the schema module — see `src/autoreplies/services/airtable_schema.py` and `scripts/generate_airtable_schema.py`. The CURATED dict is the contract.
- **Inquiries field semantics:** `Method = "Web"` (not `"Email"`); `Type (Non Website)` has no hyphen; `Agent` is a lookup field, never written. Same as production.
- **Zillow has no first_name and no phone.** Don't gate on either. Test fixtures and the Drafts table must render gracefully when both are null.
- **Reply destination order:** `Reply-To:` header → `parsed.email` → skip with reason. Same logic as PLAN.md § 4.
- **Never call `gmail.users.send` from the harness path.** The `DraftSend` strategy materializes the would-have-sent reply as a Drafts row instead.
- **Never write to prod Airtable, prod Supabase, or `#platform-leads` from the harness path.**
- **Use `pyairtable.formulas`** (`AND`, `OR`, `EQ`, `FIND`, `Field`) to construct Airtable filter formulas. No raw f-string interpolation.

### Distinctness invariants (production must remain independent of the harness)

The harness reuses production code by design (parsers, matchers, template-fill). The reverse must never be true.

1. **Production behavior unchanged.** After H1, `pipeline/process_lead.py` must still raise `NotImplementedError` for Phase A/B/C bodies *when wired with production strategies*. The strategy refactor is structural only — it does not implement any production phase as a side effect. PLAN.md Phases 1–4 are separate work.
2. **Production and harness wiring live in separate factories.** Add `pipeline.build_production_pipeline()` (raises `NotImplementedError` until PLAN.md Phase 1) and `harness.pipeline.build_harness_pipeline()` (the actually-wired one). Neither factory imports the other's strategies; the harness never reaches into production wiring to monkey-patch around stubs.
3. **CI enforces the boundary.** [`tests/test_distinctness.py`](./tests/test_distinctness.py) walks every `autoreplies.*` module outside the harness namespace and asserts no `autoreplies.harness.*` lands in `sys.modules` as a side effect. Don't disable it; if it trips, fix the import.
4. **No harness env defaults leak to production.** No production code path may read `AIRTABLE_TEST_BASE_ID` or any `HARNESS_*` setting. The `harness_airtable_base_id` property in `config.py` raises if unset when invoked — production code never invokes it.

### `parser_used` taxonomy

`ParsedLead.parser_used` keeps source-named values: `"streeteasy"`, `"zillow"`, `"llm_fallback"`. The Drafts table's `Parser Used` single-select uses `regex` / `llm_fallback`. **Mapping (`"streeteasy"` / `"zillow"` → `"regex"`) lives in `DraftSend`, not in `ParsedLead`** — production analytics keeps the richer taxonomy.

---

## Pre-H1 cleanup (already complete)

Four production-product fixes landed before the harness build began. Don't redo them; just be aware they are in place:

1. `SupabaseClient.upsert_inquiry` default `method="Web"` (was `"Email"` — a bug versus PLAN.md § 6).
2. `AirtableClient` accepts `address_match_threshold` at construction; `deps.get_airtable_client` passes `settings.apartment_fuzzy_match_threshold`. Per-call `threshold` arg still overrides.
3. `autoreplies.logging_config.configure_logging(level)` is the one place to configure logging. `main.py`, `workers/worker.py`, and `workers/scheduler.py` all use it.
4. `ParsedLead.parser_used` taxonomy resolved (see "Distinctness invariants" above).

## Build order (each phase = one PR-shaped unit)

### Phase H1 — Strategy refactor of the production pipeline (no behavior change)

Refactor the three side-effect call sites in `pipeline/process_lead.py` to go through small protocols. Production wires `Live*`; harness will wire `Draft*`/`Noop*` later.

- Create `src/autoreplies/pipeline/strategies.py` with `SendStrategy`, `SlackStrategy`, `SupabaseStrategy` protocols (use `typing.Protocol`).
- Implement `LiveSend`, `LiveSlack`, `LiveSupabase` thin wrappers around the existing service clients. They can keep raising `NotImplementedError` — Phase 0 is still the production-stub status.
- Refactor `pipeline/process_lead.py` to take strategies via constructor or dependency function. Don't change phase boundaries (A/B/C) or `JobState`.
- Acceptance: `make test` is green; production pipeline still raises `NotImplementedError` at the phase bodies; the strategies plumb correctly when injected with mocks.

### Phase H2 — Schema regeneration with `--base` flag, plus Drafts table

- Extend `scripts/generate_airtable_schema.py` to accept `--base PROD` or `--base TEST` (default `PROD`). Emit *both* `PROD` and `TEST` `PearTrackerSchema` instances into `src/autoreplies/services/airtable_schema.py` when `TEST` is passed, leaving the existing `PROD` entry untouched. `SCHEMAS` dict resolves by `base_id`.
- Extend the CURATED dict to declare a new **Drafts** table (test base only) with the fields from `TESTING_HARNESS_PLAN.md` § 3. Add a `DraftsTable` dataclass to the schema module mirroring the existing `InquiriesTable` style.
- Add `airtable_test_base_id` to `config.py` (read from env). Keep `active_airtable_base_id` working for production use; add a `harness_airtable_base_id` property that *requires* the test base ID (blow up loudly if unset when in harness mode).
- Acceptance: a TEST schema entry appears in `airtable_schema.py` after running `python scripts/generate_airtable_schema.py --base TEST` against a test base whose ID is set in env; existing PROD entry unchanged; `get_schema(base_id)` resolves both.

#### Resolved details from Sam (do not re-ask)

- **Test base ID:** `appmSm1FyerysvtcX`. Set as `AIRTABLE_TEST_BASE_ID` in `.env` before running the regen.
- **Synced source-record-id fields** (used by H5 translator):
  - `Apartments` table — display name **"Record"**.
  - `Users` table — display name **"Record ID"**.
  - Both should be exposed in CURATED as py_ident `source_record_id` (via the tuple override below) so H5 translator code can reference `schema.apartments.source_record_id` and `schema.users.source_record_id` uniformly.
- **Test base setup is complete:** synced Apartments + Users, writable Inquiries mirroring prod fields, and Drafts table per TESTING_HARNESS_PLAN.md § 3. The PAT in `.env` has access to the test base.
- **Pre-existing `airtable_staging_base_id`** stays. It serves a different purpose (dev environments using a non-prod base for manual testing). Don't repurpose it.

#### CURATED dict — new shape

Current shape (`dict[str, list[str]]`) can't express test-only tables or test-only fields. Migrate to:

```python
@dataclass
class TableSpec:
    fields: list[str | tuple[str, str]]                          # (display_name, py_ident) overrides bare-string auto-derivation
    test_only_fields: list[str | tuple[str, str]] = field(default_factory=list)
    bases: tuple[str, ...] = ("PROD", "TEST")                    # which bases this table appears in

CURATED: dict[str, TableSpec] = {
    "Users": TableSpec(
        fields=["Email", "Type", "Name", "Phone", "Autoreply (Agent)"],
        test_only_fields=[("Record ID", "source_record_id")],
    ),
    "Apartments": TableSpec(
        fields=["Streeteasy", "Full Address", "Apartment"],
        test_only_fields=[("Record", "source_record_id")],
    ),
    "Inquiries": TableSpec(
        fields=[
            "Name (Form)", "Email (Form)", "Phone", "Message",
            "Apartment", "User", "Method", "Type (Non Website)",
            "Gmail Message ID (Autoreply)",
        ],
    ),
    "Drafts": TableSpec(
        fields=[
            # Per TESTING_HARNESS_PLAN.md § 3 — confirm exact display names
            # against the test base before emitting.
            "Inquiry", "Recipient", "Subject", "Body Plaintext", "Body HTML",
            "Source", "Parser Used", "Template Source", "Reply Route",
            "Skipped Reason", "Apartment Match Strategy",
            "Apartment Match Confidence", "LLM Model", "LLM Latency Ms",
            "Would Send At", "Notes / Warnings", "Gmail Message ID",
        ],
        bases=("TEST",),
    ),
}
```

Generator rules:
- When generating for a base whose key is not in `TableSpec.bases`, skip the table entirely.
- For bases that *are* in `TableSpec.bases`, emit `fields + test_only_fields` if the base is `TEST`, otherwise emit `fields` only.
- Tuple form `("Display Name", "py_ident_override")` produces the explicit identifier; bare string falls through `to_py_ident()` as today.

#### `--base` flag semantics

Always fetch every base whose ID is configured in `.env` (`AIRTABLE_BASE_ID`, `AIRTABLE_STAGING_BASE_ID`, `AIRTABLE_TEST_BASE_ID`). The `--base` flag is a *validation gate* — it asserts the named base is configured and present in the run, and fails loudly if not. PROD's emission is byte-stable across reruns because PROD metadata doesn't change between calls, so "PROD entry unchanged" is satisfied without parsing the existing module.

#### Inquiries table in the test base — note for Sonnet

The test base's Inquiries table is a copy of the prod Inquiries table and contains extra columns the harness doesn't touch (form-platform fields like credit/income/budget/etc.). CURATED's allowlist scopes the generator to only the fields named above; the extras are invisible. No cleanup required.

### Phase H3 — `AirtableClient.create_draft`

- Add `create_draft(...)` to `src/autoreplies/services/airtable.py`, signature matching the Drafts table fields in `TESTING_HARNESS_PLAN.md` § 3.
- It accepts `inquiry_record_id` (not the parsed lead) and writes a single row linked to the test Inquiries table.
- Idempotency: the harness pipeline guarantees one Draft per Inquiry. No special handling needed inside `create_draft` — but `create_inquiry` should be wrapped by a `find_or_create_inquiry` in the harness path that uses `find_inquiry_by_gmail_message_id` first (this method already exists).
- Acceptance: unit tests around the field mapping (mock pyairtable). Anonymized fixture parse → `create_draft` produces a fields-dict containing every Drafts column with the right values.

### Phase H4 — `harness/` package

Create `src/autoreplies/harness/`:

```
harness/
  __init__.py
  state.py     # SQLite state store (sqlite3, no ORM)
  poller.py    # gmail history.list polling loop
  pipeline.py  # builds the harness-mode pipeline (DraftSend/NoopSlack/NoopSupabase)
  runner.py    # CLI dispatch — argparse with subcommands
  diff.py      # cross-base comparison
  stats.py     # aggregate metrics
```

**`state.py`:**
- `class HarnessState`: opens a SQLite file path from settings. Methods: `get_last_seen(mailbox) -> int | None` (returns unix-ms `internalDate`), `set_last_seen(mailbox, internal_date_ms)`, `was_processed(message_id)`, `mark_processed(message_id, mailbox, inquiry_id, draft_id, error=None)`.
- Schema:
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
- Use `sqlite3` stdlib. Write-ahead-log mode (`PRAGMA journal_mode=WAL`). `CREATE TABLE IF NOT EXISTS` on init.

**`poller.py`:**
- `discover_agent_mailboxes(airtable_client) -> list[str]`: read TEST base Users table, `Type = "Agent"`, project `Email`. Cache for the worker lifetime; refresh on `SIGHUP` or every 6 hours.
- `LEAD_SENDER_QUERY = 'from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com)'` — module constant. Sender allowlist mirrors PLAN.md § 1.
- `poll_once(mailbox, gmail_client, state)`:
  - On first call (`get_last_seen(mailbox)` is None), set `last_seen = current_time_ms - settings.harness_bootstrap_lookback_seconds * 1000` (default 60s). Don't backfill — that's a separate subcommand.
  - Build query: `q = f"{LEAD_SENDER_QUERY} after:{int(last_seen / 1000)}"`. Gmail's `after:` operator takes seconds.
  - Call `gmail.users.messages.list(userId=mailbox, q=q, maxResults=100)`. Page if `nextPageToken` is returned.
  - For each message-id, skip if `was_processed`. Otherwise dispatch to `pipeline.run(message_id, mailbox)`. Track the max `internalDate` seen across this batch.
  - After processing the batch, `set_last_seen(mailbox, max_internal_date_ms)`.
  - No 404/history-expiry edge case (messages.list has no history-id concept).
- `run_forever(interval_seconds)`: loop per-mailbox sequentially with a small inter-mailbox sleep to spread Gmail API calls. Handle SIGTERM cleanly — finish the in-flight message, then exit.

**`pipeline.py`:**
- `build_harness_pipeline()`: wires the same `pipeline.process_lead` orchestration but with `DraftSend`, `NoopSlack`, `NoopSupabase` strategies (override default for `--write-supabase` and `--slack-channel` flags). The Airtable client is constructed against `settings.airtable_test_base_id`.
- The harness pipeline calls `find_or_create_inquiry(...)` (per H3) so re-runs of the same message-id idempotently re-use the existing inquiry rather than creating a duplicate.
- `DraftSend.send_reply(...)` constructs a Drafts row from the args plus parser/match/llm metadata threaded through `JobState.extra` and calls `airtable.create_draft(...)`. Returns a fake send-result so the orchestration code is unchanged.

**`runner.py`:**
- `python -m autoreplies.harness <subcommand>`. Subcommands: `watch`, `backfill`, `replay`, `diff`, `stats`. Each implemented as a function with its own argparse subparser.
- `watch`: starts `poller.run_forever(...)`.
- `backfill --since YYYY-MM-DD [--mailbox X@pearnyc.com] [--limit N]`: enumerate via `messages.list(userId=mailbox, q=f"{LEAD_SENDER_QUERY} after:<unix_ts>")`. Process each through the harness pipeline. Skip already-processed (state).
- `replay <gmail_message_id> --mailbox X@pearnyc.com`: bypass state; force-process one message. Useful for debugging.

**`diff.py` / `stats.py`:** see Phase H5.

### Phase H5 — Diff + stats reports (with cross-base ID translator)

**`translator.py`** — needed because prod record IDs and synced test base record IDs differ.

- `class RecordIdTranslator`: build at construction time. For each synced table (Apartments, Users, Buildings, Management) in the test base, read all rows projecting `(prod_record_id_field, test_record_id)`. The `prod_record_id_field` is the synced column that surfaces the prod `RECORD_ID()` formula. Cache as two dicts per table: `prod_to_test: dict[str, str]` and `test_to_prod: dict[str, str]`.
- API: `translator.apartment_prod_to_test(prod_rec_id) -> str | None`, `translator.apartment_test_to_prod(test_rec_id) -> str | None`, and likewise for users.
- The schema module needs the new field ID for the prod-record-id column on each synced table — add to CURATED in Phase H2 (one ID per synced table: `apartments.source_record_id`, `users.source_record_id`, etc.). These IDs only exist on the TEST base entry in `SCHEMAS`.
- **Pre-flight check at startup:** if any synced table is missing the source-record-id column (Sam hasn't added the `RECORD_ID()` formula in prod and re-synced yet), the translator logs a loud warning and operates in degraded mode (returns None for every lookup). The diff tool then falls back to address-based comparison.

**`diff.py`:**
- Read prod Inquiries (PROD schema, prod base) and test Inquiries (TEST schema, test base) for the date window. Join on `Gmail Message ID (Autoreply)` if both rows have it; fall back to `(Email (Form), listing URL substring)` for older prod rows that pre-date the new field.
- For each joined pair, emit one CSV row: `gmail_message_id, in_prod, in_test, prod_apartment_id, prod_apartment_id_translated_to_test, test_apartment_id, apartment_match_agreement (yes/no/translator_missing), prod_user_id, test_user_id_translated_to_prod, user_match_agreement, parsed_first_name_match, parsed_phone_match, message_match, notes`.
- `apartment_match_agreement = yes` when `translator.apartment_prod_to_test(prod_apartment_id) == test_apartment_id`. `no` when both sides matched something but to different records. `translator_missing` when the translator is in degraded mode.

**`stats.py`:**
- Aggregate over the date window — counts by source, parser_used breakdown, apartment match strategy distribution (`streeteasy_id` / `address` / `none`), % skipped reply routes, p50/p95 LLM latency, template_source distribution. Print as a table; no CSV needed.

### Phase H6 — Docker Compose service + Make targets

- Add `harness-poller` service to `docker-compose.yml`, reusing the `worker` image, command `python -m autoreplies.harness watch`, env file `.env.harness`. `restart: unless-stopped`. Mount a named volume at `/var/lib/pear-autoreply` for the SQLite state file.
- Make targets:
  - `make schema-regen ARGS="--base TEST"` → `python scripts/generate_airtable_schema.py --base TEST`
  - `make harness-watch` → `docker compose up -d harness-poller`
  - `make harness-backfill SINCE=YYYY-MM-DD` → `docker compose run --rm harness-poller python -m autoreplies.harness backfill --since $(SINCE)`
  - `make harness-replay MID=...` → likewise
  - `make harness-diff SINCE=YYYY-MM-DD OUT=...`
  - `make harness-stats SINCE=YYYY-MM-DD`
- Update `.env.example` with the new harness vars: `AIRTABLE_TEST_BASE_ID=appmSm1FyerysvtcX`, `HARNESS_POLL_INTERVAL_SECONDS=60`, `HARNESS_BOOTSTRAP_LOOKBACK_SECONDS=60`, `HARNESS_STATE_PATH=/var/lib/pear-autoreply/harness.sqlite`. **No** `HARNESS_LEAD_LABEL_ID` — query-based polling doesn't need it.

### Phase H7 — Tests

- Unit tests for `state.py` (sqlite roundtrip), `poller.py` (mock the Gmail client), `pipeline.py` (assert `DraftSend` writes to Drafts and not Gmail), `diff.py` (synthetic prod+test datasets, expected diff CSV).
- Integration test using anonymized fixtures from `fixtures/anonymized/` (which Sonnet should produce a small set of as part of Phase H4 if not already present — Sam has 3 StreetEasy + 3 Zillow raw fixtures locally; he'll provide if needed).

---

## Resolved inputs (no need to ask)

- **TEST base ID:** `appmSm1FyerysvtcX` — put this in `.env.example` as `AIRTABLE_TEST_BASE_ID`.
- **Gmail ingestion strategy:** **query-based polling, not label-based.** Production will install the `Pear/Leads` filter+label at cutover (PLAN.md § 1). The harness skips that dependency entirely and polls with `gmail.users.messages.list(userId=mailbox, q="from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com) after:<unix_ts>")`. State tracking is a single `last_seen_internal_date_unix_ms` per mailbox. This is a deliberate divergence from PLAN.md's `history.list(labelId=...)` path — the harness validates parser/matcher/LLM/Airtable correctness, not the label plumbing.
- **Native-sync record IDs:** **prod IDs and test IDs differ.** At harness runtime this is not an issue — the matchers query the test base directly and get test-base IDs natively. The translator is needed *only* in the diff tool for cross-base comparison. See Phase H5 below.
- **Docker restart policy:** `restart: unless-stopped`.

## Things you may still need to ask Sam

1. Confirmation that he's added the **`Record ID` formula field** (`RECORD_ID()`) to prod Apartments, Users, Buildings, Management tables and re-synced to the test base. If not, the diff tool can't translate prod→test record IDs and the comparison falls back to address-based matching only — flag this if it's still pending when you start Phase H5.
2. If the Drafts table schema needs any additional diagnostic columns Sam wants to surface to sales reviewers (e.g. a "review status" select for sales to mark drafts as ✓ / ✗ / unclear). Default: ship the columns in TESTING_HARNESS_PLAN.md § 3 verbatim and let him add a review-status column manually if he wants one.

---

## Out of scope for this build

- Production Pub/Sub webhook (`pipeline/process_lead.py` Phase A body), production Gmail send, production Slack, production Supabase. Those are Phases 1–5 of `PLAN.md` — leave the stubs as `NotImplementedError`.
- Any change to `legacy/zapier_supabase_post.py`.
- Migrations of historical Zapier-handled leads into the test base.
- Watch-renewal cron for production Pub/Sub (the harness uses polling, no watch needed).

---

## Definition of done

- `docker compose up -d harness-poller` runs continuously on the prod droplet, polling every 60s, writing Drafts to the test base, and writing nothing else anywhere.
- `make harness-backfill SINCE=2026-04-29` populates the test base with 7 days of leads end-to-end without errors.
- `make harness-diff SINCE=...` and `make harness-stats SINCE=...` produce a clean CSV / printed table.
- All tests pass (`make test`).
- The production code paths are untouched in observable behavior — same `NotImplementedError`s in the same places.
- README updated with a "Testing harness" section pointing to `TESTING_HARNESS_PLAN.md` and a quickstart for the make targets.
