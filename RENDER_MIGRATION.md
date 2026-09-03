# Render Migration Runbook — Pear Autoreplies

This document describes the migration of the Pear Autoreplies stack from a
DigitalOcean droplet (single Docker Compose host) to Render.

> **Status (2026-09-02): migration complete, droplet deleted.** Render is the only
> deployment target. Every `ssh root@161.35.13.81` step below is historical and
> cannot be run; the "DO as fallback" rollback path in §6 no longer exists. The
> droplet's IP was recycled to a third party while `autoreplies.pearnyc.com` still
> pointed at it, which was exploited as a dangling-DNS subdomain takeover (see
> CLAUDE.md → *Production*). The remaining action from §5 is the DNS record
> itself: remove the stale A record, or CNAME the hostname to the Render `-web`
> service as a Custom Domain.

**Current state (read this first):** DO is running the **testing harness**, which
writes to the TEST Airtable base — **production has not launched anywhere yet.**
So this migration is two distinct moves:

1. **Hand the harness off** from DO to Render (stop DO's harness-poller, start
   Render's). Both write the same TEST base, so they must never run at once.
2. **Launch production for the first time, on Render** (enable the prod poller).
   This is a first-time launch, not a cutover from a running DO prod system.

DO's role is therefore (a) the harness to hand off, and (b) a **fallback launch
target** for production if Render misbehaves — not a live system to "restore".
Keep it deployable until production is stable on Render.

---

## 1. Overview

### Service map

| DO Compose service | Render service type | Notes |
|---|---|---|
| `web` (FastAPI/uvicorn behind Caddy) | `type: web` — `pear-autoreplies-web` | Render terminates TLS; no Caddy needed |
| `worker` (RQ drainer) | `type: worker` — `pear-autoreplies-worker` | `numInstances: 2`; `dockerCommand` overrides CMD |
| `scheduler` (Gmail watch renewal) | **Not deployed** | Watch-renewal serves only the Pub/Sub push path; prod polls, so it's a no-op (see §7) |
| `poller` (production Gmail poller) | `type: worker` — `pear-autoreplies-poller` | `numInstances: 1`; SQLite on persistent disk |
| `harness-poller` (testing harness) | `type: worker` — `pear-autoreplies-harness-poller` | `numInstances: 1`; own persistent disk; no Redis |
| `redis` (RQ + idempotency) | `type: keyvalue` — `pear-autoreplies-redis` | `plan: starter` for persistence; `noeviction` policy |
| `caddy` (TLS + routing + rate-limit) | **Dropped** | Render handles TLS/routing; `/admin/*` rate-limit moved to `src/autoreplies/ratelimit.py` |

### Goal

Move the testing harness to Render and validate parity (parse/match/template) against the DO harness's known-good output, then **launch production for the first time on Render**.  Production has never run on DO, so this is a launch, not a cutover.  Keep DO deployable as a fallback launch target until Render is stable.

---

## 2. One-time Render setup

### Connect the repo and create the Blueprint

1. Log into the Render dashboard and go to **Blueprints → New Blueprint**.
2. Connect the GitHub repo: `github.com/seboyer/pear-autoreply`.
3. Point it at `render.yaml` in the repo root.  Render will create all declared services in the `virginia` region.

### Add secrets to the env group (manually — `sync: false` does NOT work in groups)

**Render ignores `sync: false` on env vars declared inside an environment
group** — it silently drops them. This is what caused the `AIRTABLE_TOKEN` 401
on the first poller boot. So the secrets are **not** declared in `render.yaml`
at all; add them by hand:

**Dashboard → left nav → Env Groups → `pear-autoreplies-config` → Add
Environment Variable.** Every service links this group via `fromGroup`, so each
secret entered here reaches web/worker/poller/harness-poller at once.

| Key | Value |
|---|---|
| `ADMIN_TOKEN` | Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `AIRTABLE_TOKEN` | Existing PAT from `.env` (covers prod + test bases) |
| `ANTHROPIC_API_KEY` | Existing key from `.env` |
| `SUPABASE_SERVICE_ROLE_KEY` | Existing key from `.env` |
| `SLACK_BOT_TOKEN` | Existing token from `.env` |
| `PUBSUB_SERVICE_ACCOUNT_EMAIL` | The **GCP service-account email** that signs Pub/Sub push OIDC tokens (e.g. `pubsub-pusher@<project>.iam.gserviceaccount.com`) — **not** a human/admin address. Optional for now: `/pubsub/inbox` is a stub that doesn't verify the JWT yet, and the live path is the poller, so nothing consumes this until Phase 1. |

### Add the Google service-account JSON as a Secret File

Render Secret Files are configured per-service in the dashboard (not declarable
in `render.yaml`). For each service that talks to Gmail:
**Environment tab → Secret Files → Add Secret File**.

- **Filename:** `sa.json` — Render rejects absolute paths and always mounts
  Secret Files at `/etc/secrets/<filename>`, so the file lands at
  `/etc/secrets/sa.json`. Do **not** use the DigitalOcean volume path
  `/etc/pear-autoreply/sa.json` here.
- **Contents:** the full JSON of the Google service-account key with
  domain-wide delegation.

The env group sets `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/sa.json` to match
this mount path. (On DigitalOcean the same file is volume-mounted at
`/etc/pear-autoreply/sa.json` — the two platforms use different paths, set via
the env group vs `.env`.)

Add the Secret File to:
- `pear-autoreplies-harness-poller` — now (Stage 1; it reads Gmail).
- `pear-autoreplies-poller` and `pear-autoreplies-worker` — at cutover (§5).

(`pear-autoreplies-web` does not need it — it serves only `/healthz` + `/admin`,
no Gmail. `pear-autoreplies-scheduler` is not deployed — see §7.)

### Redis plan note

The `pear-autoreplies-redis` Key Value service uses `plan: starter`.  The
**free** plan does not persist data across restarts — RQ jobs would be lost on
every Redis restart.  Paid plans (`starter` and up) persist to disk
(`appendfsync everysec`), required for durability parity with the DO Redis
container.

Where to set / verify the plan:
- **Declaratively:** `plan: starter` on the `keyvalue` service in `render.yaml`
  (already set).
- **Dashboard:** the Key Value instance → **Info** page → **Key Value Instance**
  section → **Update** under **Instance Type**.

⚠️ Confirm the instance is **not Free** before relying on it — and note that
**upgrading a Free instance wipes its data**, so pick a paid plan from the start.
`maxmemoryPolicy: noeviction` (so RQ jobs are never evicted) is set in the
Blueprint and editable in the instance settings.

### Known risk — non-root disk write permissions

The Docker image runs as a **non-root user** (`app`, uid 999 — see the
`Dockerfile` runtime stage).  On DigitalOcean this required a manual
`chown 999:999` on the state volume (see the CLAUDE.md "Production" /
first-deploy note).  Render does **not** document the ownership of a mounted
persistent disk, so the `poller` and `harness-poller` may hit
`PermissionError` / "unable to open database file" when they try to create
their SQLite file under `/var/lib/pear-autoreply`.

This is **caught safely** by the staged rollout: the `harness-poller` is the
first disk-writing service brought up in Stage 1 (zero client risk), so a
permission failure surfaces loudly in its logs during validation — never at
cutover.  See the first item of the §4 checklist.

**Mitigation if it fails:** the fix is a root entrypoint that `chown`s the
mount and drops back to `app` (e.g. add an `ENTRYPOINT` script using
`gosu`/`su-exec` to the shared `Dockerfile`, keeping it backward-compatible
with the DO deploy).  Do **not** apply this speculatively — only if the
Stage-1 logs show the disk is not writable.

---

## 3. Staged rollout (zero client risk)

Two facts make this safe to stage:
- The only component that sends live replies is the **`poller`**.  The `web`
  Pub/Sub route is a Phase-0 stub that acks without processing.  As long as the
  poller stays suspended, nothing can send.
- DO and Render **harness-pollers target the same mailboxes and write the same
  TEST base** (`appmSm1FyerysvtcX`).  Running both = **duplicate Drafts rows**,
  so the harness move is a hand-off, not a parallel bring-up.

### Stage 1 — Harness hand-off + platform validation

1. In the Render dashboard, **suspend** `-poller` and `-worker`, and do **not**
   add their SA Secret File yet.
2. Add the secrets to the env group (§2) and the SA Secret File (`sa.json` →
   `/etc/secrets/sa.json`, see §2) to **`-harness-poller` only**.
3. **Stop the DO harness first** so it stops writing the TEST base:
   ```bash
   ssh root@161.35.13.81
   cd /opt/pear-autoreplies/app && docker compose stop harness-poller
   ```
4. Bring up Render `-redis` + `-web` + `-harness-poller`.  The Render harness is
   now the **sole** writer to `appmSm1FyerysvtcX`.
5. Run the §4 checklist.  Fill any hand-off gap (leads between DO-stop and
   Render-start beyond the 60s bootstrap lookback) with
   `python -m autoreplies.harness backfill --since <stop-time>`, scoped to the
   gap **only** — overlapping a window DO already wrote duplicates rows.

> For a true side-by-side parity diff, point the Render harness at a **separate
> scratch base** via `AIRTABLE_TEST_BASE_ID`, run both in parallel, diff, then
> switch back to `appmSm1FyerysvtcX` and do the hand-off above.

### Stage 2 — Production launch (see §5)

Only proceed after the §4 checklist passes.

---

## 4. Validation checklist

Execute these checks after Stage 1 is running.  Check the box and record the
result when each passes.

> **Validation status — 2026-06-08 (harness validated on Render; ready to cut over):**
> - Disk writable by non-root `app` user ✓ (harness opened `harness.sqlite` on the mounted disk)
> - SA file working ✓ — added as `sa.json`, mounts at `/etc/secrets/sa.json`, with
>   `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/sa.json` in the env group; harness READS Gmail
> - `AIRTABLE_TOKEN` resolves, no 401 ✓ · harness materializing Drafts into the TEST base ✓
> - **False-positive guard ✓** — `scripts/check_prod_inbox_leads.py` sampled the production
>   inboxes and confirmed the autoreply would NOT trigger on any existing mail, so enabling the
>   poller won't reply to old messages (the 60s `POLLER_BOOTSTRAP_LOOKBACK_SECONDS` also bounds this).

- [ ] **Disk is writable by the non-root user** (check FIRST) — Confirm the
  `harness-poller` successfully creates/opens its SQLite file at
  `/var/lib/pear-autoreply/harness.sqlite`.  In the Render service logs there
  should be **no** `PermissionError` / "unable to open database file".  If it
  fails, apply the mitigation in §2 "Known risk — non-root disk write
  permissions" before continuing — everything downstream depends on this.
  _Result: pending_

- [ ] **Harness parity** — With the DO harness stopped (per §3), the Render
  `harness-poller` is the sole writer to the TEST base (`appmSm1FyerysvtcX`).
  Confirm its new Drafts rows match the shape/values of the DO harness's prior
  known-good rows for comparable leads (same parsers/matchers/templates →
  identical output).  `python -m autoreplies.harness stats --since <date>`; use
  the scratch-base side-by-side diff (§3) if you want a direct A/B.
  _Result: pending_

- [ ] **Web service reachable** — `/healthz` returns HTTP 200 over HTTPS on
  the Render `.onrender.com` URL.  Confirm TLS is auto-provisioned and the
  certificate is valid.
  _Result: pending_

- [ ] **SA domain-wide delegation from Render** — Confirm the harness-poller
  can READ Gmail (it lists messages for at least one monitored mailbox without
  errors in the Render service logs).  Do NOT test SEND from Render until
  cutover — the controlled send test is part of §5.
  _Result: pending_

- [ ] **Redis connectivity** — Briefly un-suspend `-worker` (no SA file or
  running poller needed for this) and confirm `REDIS_URL` resolves from the env
  group and the worker connects to the Key Value instance with no errors in the
  logs.  Re-suspend it afterwards.
  _Result: pending_

- [ ] **SQLite state survives redeploy** — Note the current harness-poller
  cursor timestamp.  Trigger a redeploy of `pear-autoreplies-harness-poller`
  from the dashboard.  Confirm: (a) the service resumes without re-processing
  the messages already handled (no duplicate Drafts rows appear), and (b) it
  does not skip forward past any unprocessed messages.
  **Warning:** harness dedup is sticky on failure (see CLAUDE.md).  If the
  Render harness double-processes or skips during validation, recover by
  deleting failed rows from `processed_messages` (`DELETE FROM
  processed_messages WHERE error LIKE '%<class>%'`) and rolling back the
  affected `mailbox_state` cursors in the SQLite file on the persistent disk.
  Seed the initial cursor carefully so you do not re-process the window the
  DO harness already covered.
  _Result: pending_

---

## 5. Production launch (first-time live sends)

Only proceed after all §4 checklist items pass.  This is the actual go-live.

> **Launch record — 2026-06-10 (production live on Render; quiet pre-changeover state):**
> - SA Secret File `sa.json` added to `-worker` (the poller already had it); both now
>   mount `/etc/secrets/sa.json`.
> - **Pre-resume safety gate re-run fresh:** `scripts/check_prod_inbox_leads.py` →
>   **0 triggering leads** across all 7 monitored mailboxes (dana, jair, jordan, mike,
>   richard, robert, shayna). The prod poller had a stale cursor from a brief
>   2026-06-07 22:15–22:27 live run (only deploy before this), but the clean 30-day
>   check window fully covers the replay window, so resume replays nothing — confirmed
>   empirically by `fetched=0` on every mailbox after bring-up.
> - **DO quiesced:** `worker` + `scheduler` stopped (no `poller` service exists on DO);
>   `harness-poller` already `Exited` 44h prior; `web`/`caddy`/`redis` left up (live domain
>   + rollback fallback).
> - **Render bring-up:** `-worker` resumed first (RQ `Listening on default` ×2 → Redis OK,
>   no Airtable 401, no errors), then `-poller` (polls all 7 mailboxes, `fetched=0`
>   everywhere → disk writable, SA read works, 0 sends). `/healthz` → HTTP 200 over HTTPS.
> - **Deployed commit verified:** worker + poller both live on `31d03e1` (#19), which has
>   #16 (`f0f5588`, the template-field swap) and #17 as ancestors, and whose `Dockerfile`
>   COPYs `FALLBACK_TEMPLATE.md` (not `.dockerignore`d) — so the template + fallback paths
>   are correct in the running image. (`autoDeploy: false` means resume could have pinned an
>   older commit; it did not — it rebuilt connected-branch HEAD.)
> - **Supabase key pre-checked:** read-only PostgREST probe of `inquiries` → HTTP 200 with
>   the new-format `sb_secret_…` key (REST-compatible). The Supabase write is otherwise
>   never exercised before the first real lead (harness uses a Noop Supabase strategy).
> - **`ADMIN_TOKEN` set (2026-06-10):** added as a **web-service-level** env var (NOT the
>   group, so poller/worker/harness didn't churn). Verified on `/admin/healthz/detail`:
>   no token → 401, old default `dev-token-change-me` → 401, new token → 200. The default-token
>   exposure on the public `.onrender.com` URL is now closed. Value recorded in
>   `~/Dev/1-Resources/master.env` as `ADMIN_TOKEN_AUTOREPLIES` (maps to the `ADMIN_TOKEN`
>   env var the app reads). Web is now on `31d03e1` too.
>   ⚠️ **Gotcha:** with `autoDeploy: false`, adding/editing an env var does **not** auto-deploy
>   the service — the value is staged but the running container keeps the old config until you
>   trigger a deploy (`POST /v1/services/{id}/deploys` or the dashboard). The first probe after
>   the `PUT` still showed the old default working; a manual deploy was required to apply it.
> - **Still pending (user-driven):** (1) controlled single-mailbox send test — one agent
>   repoints their StreetEasy account to the production email; that first real lead is the
>   live send-path test (the worker's SA *send* scope / domain-wide delegation is
>   unexercised until then). (2) Open the remaining mailboxes. (3) DNS flip of
>   `autoreplies.pearnyc.com` → Render (Custom Domain on `-web`; `ADMIN_TOKEN` now in place).
>
> **Per-agent changeover gotchas (observed during richard@/Richard Garland's test, 2026-06-11):**
> - **The Leads-Email change requires a poller restart.** The poller caches the monitored-mailbox
>   list for 6h and SIGHUP is not exposed on Render — so after editing a User's `Leads Email` in
>   Airtable, redeploy `-poller` (`POST /v1/services/{id}/deploys`, disk/cursor persist) or wait
>   out the cache. A new mailbox bootstraps with the 60s lookback, so restart *before* leads start
>   arriving there. (richard: legacy email `garland@pearnyc.com` → new leads email `inbox@pearnyc.com`.)
> - **StreetEasy's email cutover is NOT instant.** After repointing the StreetEasy account, leads
>   kept arriving at the *old* autoreply email (`garland@`) for ~10 min (real inquiry "524 Lafayette
>   Avenue #2 / Jose Godinez" landed at `garland@` 14:55 UTC, well after the repoint). **Keep the
>   legacy autoreply system LIVE on the old mailbox through each changeover** so propagation-gap leads
>   are still answered — the new poller only watches the new leads email and will not see the old one.
>   Confirm the first lead actually lands in the new mailbox before considering that agent cut over.

### Enable the production services

1. Add the SA Secret File (`sa.json` → `/etc/secrets/sa.json`, see §2) to
   `-poller` and `-worker`.
2. **Verify `AIRTABLE_TOKEN` resolves before starting the poller.**  The env
   group must have it set (§2).  A missing/invalid token makes the poller fail
   mailbox discovery with `401 AUTHENTICATION_REQUIRED` and send nothing — this
   is what happened on the first boot.  Un-suspend `-worker` briefly and confirm
   no Airtable 401s in the logs before touching the poller.
3. **Controlled end-to-end send test.**  Note: the **harness does not send** (it
   writes Drafts to the TEST base), so a real send test must use the **prod**
   pipeline.  Point the poller at a single **test/agent mailbox you control**
   (via the monitored-mailbox list in Airtable), drop a synthetic
   StreetEasy/Zillow lead in, and confirm a reply is sent and rows land in the
   PROD base (`appwPKlnV6YtbIjWz`) + Slack `#platform-leads` + Supabase.  Then
   restore the real monitored-mailbox set.
4. **Guarantee a single prod poller** — ensure the DO prod poller cannot also run
   (even if idle today):
   ```bash
   ssh root@161.35.13.81
   cd /opt/pear-autoreplies/app && docker compose stop poller worker scheduler
   ```
5. Un-suspend `-worker` **first (or together with the poller)** — the worker is
   the sender (it runs the RQ `send_reply_job`). If the poller runs while the
   worker is suspended, the poller still writes to the PROD base / Slack / Supabase
   and queues the sends, which then **flush in a burst** when the worker starts.
   Then un-suspend `-poller`; it is now the sole production poller. Watch the first
   real leads end-to-end.

> **Template field at launch:** production reads the new `Autoreply Template (Agent)`
> field (PR #16, merged — `process_lead` resolves `autoreply_template`). Agents who
> have **not** populated it get the Pear-wide `FALLBACK_TEMPLATE`, not their legacy
> `Autoreply (Agent)` text (pure swap). Confirm which agents are migrated so you know
> who receives a personalized vs. generic first reply.

### Flip the platform endpoints

This serves `/admin` and `/healthz` — it does **not** carry live lead traffic
(the poller pulls from Gmail directly), so it can be flipped without a
send-traffic cutover.

1. **DNS (Google Domains)** — repoint `autoreplies.pearnyc.com` to Render, or add
   it as a **Custom Domain** on `-web` (CNAME → the `.onrender.com` host; Render
   auto-provisions the cert). **Do this before (or at the moment) the old host is
   decommissioned.** This step was skipped when the droplet was deleted, leaving
   the A record dangling at a recycled IP — see the status note at the top.

---

## 6. Rollback

> **Obsolete:** the DO droplet is deleted, so neither rollback path below can be
> executed. Kept for the record only. Rollback on Render today means redeploying a
> previous image from the Render dashboard.

No data migration is needed — SQLite cursors are independent per platform.  The
two moves roll back separately.

**Harness (Stage 1):** suspend the Render `-harness-poller`, then restart DO's so
exactly one harness writes the TEST base:
```bash
ssh root@161.35.13.81
cd /opt/pear-autoreplies/app && docker compose start harness-poller
```
`backfill --since` any gap on whichever harness resumes.

**Production (Stage 2):** suspend the Render `-poller` **first** (single-poller
invariant), then launch production on DO as the fallback:
```bash
cd /opt/pear-autoreplies/app && git pull && docker compose up -d
```
Revert DNS to DO.  If the Render
poller already sent for some messages, those IDs are not in DO's
`processed_messages`, so DO could re-send them — if overlap matters, copy the
relevant `processed_messages` rows from the Render disk (via `render disk
download` or shell access) into the DO SQLite before starting the DO poller.

---

## 7. Scheduler — not deployed (and why)

`workers/scheduler.py` renews Gmail `users.watch` subscriptions on a 24h loop.
`users.watch` exists **only** to drive the Pub/Sub **push** ingestion path —
which production does not use (it polls; see PLAN.md §1 implementation note).
So the scheduler has nothing to do: its `_run_once()` is currently a no-op.

It is therefore **omitted from `render.yaml`** rather than deployed as an
always-on worker doing nothing.  `workers/scheduler.py` stays in the codebase.

If the Pub/Sub push path is ever revived, re-add a scheduler service — either:
- a `type: worker` (always-on 24h sleep loop, zero code change), or
- a `type: cron` with `schedule: "0 8 * * *"`, which is cheaper but needs
  `workers/scheduler.py`'s loop converted to a one-shot (`_run_once()` then exit).
