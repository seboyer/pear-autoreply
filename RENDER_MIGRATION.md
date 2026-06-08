# Render Migration Runbook — Pear Autoreplies

This document describes the migration of the Pear Autoreplies stack from a
DigitalOcean droplet (single Docker Compose host) to Render.

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

### Mount the Google service-account JSON as a Secret File

Render Secret Files are configured per-service in the dashboard — they cannot
be declared in `render.yaml`.  After the Blueprint syncs, open each of the
following services and add a Secret File:

- `pear-autoreplies-web`
- `pear-autoreplies-worker`
- `pear-autoreplies-poller`
- `pear-autoreplies-harness-poller`

(`pear-autoreplies-scheduler` is not deployed — see §7.)

For each service: **Settings → Secret Files → Add Secret File**.
- **Filename (mount path):** `/etc/pear-autoreply/sa.json`
- **Contents:** paste the full JSON of the Google service-account key that has domain-wide delegation.

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
2. Add the secrets to the env group (§2) and the SA Secret File to **`-web` and
   `-harness-poller` only**.
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

### Enable the production services

1. Add the SA Secret File (`/etc/pear-autoreply/sa.json`) to `-poller` and
   `-worker`.
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
5. Un-suspend `-poller` (and `-worker`) on Render.  It is now the
   sole production poller.  Watch the first real leads end-to-end.

### Flip the platform endpoints

This serves `/admin` and `/healthz` — it does **not** carry live lead traffic
(the poller pulls from Gmail directly), so it can be flipped without a
send-traffic cutover.

1. **DNS (Google Domains)** — repoint `autoreplies.pearnyc.com` to Render, or add
   it as a **Custom Domain** on `-web` (CNAME → the `.onrender.com` host; Render
   auto-provisions the cert).

---

## 6. Rollback

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
