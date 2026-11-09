# Supabase timeouts, 29 Oct to 5 Nov 2026: impact on autoreplies

Investigated 2026-11-06 from the message-monitor handoff (`~/Dev/message-monitor/docs/autoreplies_handoff_2026-11-05.md`).
Sources: Render logs for the poller and worker (full retention, 29 Oct 00:15 UTC to 6 Nov 16:10 UTC), live
Supabase `public.inquiries`, and the production Airtable Inquiries table. Nothing was written anywhere.

## Answers

**1. Did any autoreplies fail to send? No.**

- 1,244 replies were queued (`_phase_a: done`) and 1,244 were sent (`send_reply_job: sent`). Every queued reply has a
  matching sent line, and all routes were `thread`.
- The poller logged 398 dispatch failures, which break down as follows:
  - **344 failed in phase B (the Supabase upsert), after the reply was queued.** All 344 replies went out.
  - **53 were StreetEasy non-lead emails** ("Hotsheet", "Congrats … Is Live", "Comp Report", authentication codes,
    marketing). A `ParserError` on these is correct, but they get logged as failures.
  - **1 was a Gmail 404** on 6 Nov at 06:01 (`inbox@`, `1a1af72e612fc6d1`). The message was deleted before the poller
    fetched it. This is unrelated to Supabase, and what it contained can't be recovered.
- The handoff's 222 "unclassified" rows fall into the same groups: phase-B timeouts with the reply sent, or non-lead
  emails.

**2. Is the problem ongoing? No.**

Since the upgrade at 21:54 UTC on 5 Nov:

- Zero worker failures, zero `resolve_person_id` errors and zero phase-B timeouts.
- 149 successful reply writebacks.
- 130 of 130 inquiry writes succeeded on 6 Nov.
- A REST read takes about 0.5s.

Six dispatches failed on 6 Nov, and none of them involved Supabase: five were non-lead emails and one was the Gmail
404.

**The upgrade added capacity; it didn't fix the cause.** The cause was message-monitor's unindexed backlog replay,
fixed by its PR #46. The new risk is any other heavy user of the shared project. The `SELECT COUNT(*) … FROM inquiries
WHERE date_created BETWEEN …` query (113k calls) **does not come from autoreplies**, because nothing in `src/` runs a
count query. Its owner is still unknown.

**3. What was actually lost.** The list is in `2026-11-supabase-timeouts_backfill.csv`, 376 inquiries.

| Gap | Count | Notes |
|---|---|---|
| No `public.inquiries` row | 95 | Every one is a phase-B failure. These rows also lack `reply_*`. |
| `reply_gmail_message_id` / `reply_message` / `reply_rfc822_message_id` empty | 169 | Includes the 95 above. |
| No Slack post in #platform-leads | 344 | Every phase-B failure. See below. |

- **Most timeouts happened only on the client side.** The 30s `httpx` timeout fired, but Postgres went on to finish
  the statement. 249 of the 344 failed upserts and about 267 of the 341 failed reply PATCHes actually landed. That is
  why the gap is 95 and 169 rather than 344 and 341.
- **The gap starts on 29 Oct and is fully covered by the logs:**
  - Supabase has zero autoreply rows missing reply IDs from 20 Sep through 28 Oct.
  - Airtable and Supabase match 100% for 25–29 Oct.
  - The handoff counted 156 rows with no reply before the incident, but those have no `gmail_message_id`. Some other
    writer created them, not autoreplies.
- **Slack:** phase C (the Slack post) runs only after phase B succeeds, inside the same `try` block. All 344 failures
  raised in phase B, and none was retried to `process_lead: done`. The code makes this certain. However, I couldn't
  read the channel to confirm it, because the Slack token in the local `.env` is stale (`invalid_auth`).
- **Repeat detection:** about 1,100 `resolve_person_id` timeouts failed open. Those leads got the first-touch template
  even if they were repeat inquirers. That is acceptable, and nothing needs backfilling.

## Fixes

**Correction to handoff fix #3:** don't just stop marking transient failures as processed. Persisting `JobState` is
still a stub (`_load_state` and `_save_state` are TODO no-ops), so `supabase_done` is never saved. If a failed lead
were re-dispatched, phase A would run again, and step 11 `send_reply` would queue a **second email**. Fingerprint
dedup wouldn't stop it, because it excludes the message's own ID.

1. **Phase B and C retries as separate jobs.** When phase B or C fails, queue an RQ writeback job carrying
   `airtable_record_id` and `state.extra`, with RQ `Retry` and backoff. Never re-dispatch `process_lead`. Both writes
   are already idempotent: an upsert on the primary key with `merge-duplicates`, and a PATCH on the primary key. Also
   run phase C even when phase B fails, because the Slack post shouldn't depend on Supabase.
2. **Retry the reply writeback separately from the send.** In `send_reply_job`, wrap `update_inquiry_reply`, and on
   failure queue a writeback-only job with `Retry`. The email has already gone, so the job itself must not raise and
   be retried as a whole.
3. **Treat non-lead emails as a skip.** Handle `ParserError("not a prospect lead")` the way `_NotAddressedToMailbox`
   is handled. It caused 53 of the 398 "failures" and makes the failure rate meaningless.
4. **Alert on the failure rate.** Both failure modes ran for 8 days without anyone noticing. Once fix 3 removes the
   noise, any non-zero `failed` count from the poller, or any failed worker job, is worth a Slack alert
   (`LiveSlack.post_alert` already exists).
5. **Optional: a shorter timeout plus one retry** inside `SupabaseClient`. The 30s timeout made each slow write stall
   the single-threaded poller.

**Backfill**, run from the CSV. It writes to production and has not been run:

- For the 95 missing rows: rebuild the upsert payload from the Airtable inquiry and the lead's Gmail message, which
  supplies `rfc822_message_id`.
- For all 169 reply gaps: `reply_gmail_message_id` is in the CSV, the reply body is in Airtable's Reply (Autoreply)
  field, and the RFC-822 Message-ID comes from Gmail.
- Slack backfill for the 344 is the team's call. A flood of week-old notifications may just be noise.

## Status and runbook (6 Nov)

Fixes 1–4 are implemented on branch `fix/supabase-writeback-retries`. Fix 5 (a shorter timeout) is deferred.

**Deploy the worker before the poller.** Retry jobs are queued under the import path
`autoreplies.workers.writeback_jobs.*`. An old worker can't import that path, so any job it picks up fails
immediately.

**Alerts stay off until `ALERT_SLACK_CHANNEL` is set** in the `pear-autoreplies-config` env group, and the bot has
been invited to that channel.

**Backfill:** `scripts/backfill_supabase_2026_11.py`.

- The mapping check rebuilt 281 rows phase B did write. All fields matched except 8 `user_id`s, where a User was
  linked in Airtable after the lead arrived. Reply bodies matched on all 207 rows that have one.
- The dry run plans 95 upserts and 169 reply writebacks, with 0 problems.

To run it:

1. `python scripts/backfill_supabase_2026_11.py --apply --limit 1`, then check that row in Supabase. This confirms
   `created_at` is accepted.
2. `python scripts/backfill_supabase_2026_11.py --apply`
3. `GOOGLE_APPLICATION_CREDENTIALS=<sa.json> python scripts/backfill_supabase_2026_11.py --apply --gmail` fills
   the RFC-822 Message-IDs. Only that pass helps message-monitor link reply threads.

**Done 9 Nov:** steps 1 and 2. The 95 rows were created with their original `created_at`, and all 169 reply
columns were written, with 0 problems. A follow-up dry run plans 0 writes. Step 3 is still pending: 169 rows lack
RFC-822 IDs, and it needs the Gmail service-account key.

**Unrelated items from the handoff, not investigated here:**

- `RefreshError: unauthorized_client` in the poller: one mailbox is missing domain-wide delegation.
- `~/Dev/1-Resources/master.env` lines 24 and 85 are malformed and break `set -a; . file`.
