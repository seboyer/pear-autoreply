# Render Migration Runbook — Pear Autoreplies

This document describes the migration of the Pear Autoreplies stack from a
DigitalOcean droplet (single Docker Compose host) to Render.  The DigitalOcean
droplet stays live and deployable throughout — it is the rollback target until
the Render environment passes the validation checklist below.

---

## 1. Overview

### Service map

| DO Compose service | Render service type | Notes |
|---|---|---|
| `web` (FastAPI/uvicorn behind Caddy) | `type: web` — `pear-autoreplies-web` | Render terminates TLS; no Caddy needed |
| `worker` (RQ drainer) | `type: worker` — `pear-autoreplies-worker` | `numInstances: 2`; `dockerCommand` overrides CMD |
| `scheduler` (Gmail watch renewal) | `type: worker` — `pear-autoreplies-scheduler` | 24h sleep loop; see §7 for cron alternative |
| `poller` (production Gmail poller) | `type: worker` — `pear-autoreplies-poller` | `numInstances: 1`; SQLite on persistent disk |
| `harness-poller` (testing harness) | `type: worker` — `pear-autoreplies-harness-poller` | `numInstances: 1`; own persistent disk; no Redis |
| `redis` (RQ + idempotency) | `type: keyvalue` — `pear-autoreplies-redis` | `plan: starter` for persistence; `noeviction` policy |
| `caddy` (TLS + routing + rate-limit) | **Dropped** | Render handles TLS/routing; `/admin/*` rate-limit moved to `src/autoreplies/ratelimit.py` |

### Goal

Launch all services on Render, validate via the testing harness (parse/match/template parity), then cut over production lead delivery from the DigitalOcean droplet.  Keep DO as a rollback target throughout.

---

## 2. One-time Render setup

### Connect the repo and create the Blueprint

1. Log into the Render dashboard and go to **Blueprints → New Blueprint**.
2. Connect the GitHub repo: `github.com/seboyer/pear-autoreply`.
3. Point it at `render.yaml` in the repo root.  Render will create all declared services in the `virginia` region.

### Enter secrets in the dashboard

`render.yaml` declares several env vars with `sync: false`.  These are
**not** stored in the YAML (never commit secrets).  After the Blueprint syncs,
open the `pear-autoreplies-config` env group in the dashboard and enter the
values for:

| Key | Where to find the value |
|---|---|
| `ADMIN_TOKEN` | Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `AIRTABLE_TOKEN` | Existing PAT from `.env` |
| `ANTHROPIC_API_KEY` | Existing key from `.env` |
| `SUPABASE_SERVICE_ROLE_KEY` | Existing key from `.env` |
| `SLACK_BOT_TOKEN` | Existing token from `.env` |
| `PUBSUB_SERVICE_ACCOUNT_EMAIL` | The Google SA email used for Pub/Sub push verification |

### Mount the Google service-account JSON as a Secret File

Render Secret Files are configured per-service in the dashboard — they cannot
be declared in `render.yaml`.  After the Blueprint syncs, open each of the
following services and add a Secret File:

- `pear-autoreplies-web`
- `pear-autoreplies-worker`
- `pear-autoreplies-scheduler`
- `pear-autoreplies-poller`
- `pear-autoreplies-harness-poller`

For each service: **Settings → Secret Files → Add Secret File**.
- **Filename (mount path):** `/etc/pear-autoreply/sa.json`
- **Contents:** paste the full JSON of the Google service-account key that has domain-wide delegation.

### Redis plan note

The `pear-autoreplies-redis` Key Value service uses `plan: starter`.  The
**free** plan does not persist data across restarts — RQ jobs would be lost on
every Redis restart.  The `starter` plan persists and is required for
production durability parity with the DO Redis container.

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

The only component that sends live replies to prospects is the `poller` worker.
The `web` service (Pub/Sub push route) is a Phase-0 stub that acks without
processing.  This means the Render environment can be brought up and validated
without any risk of sending duplicate or premature replies.

**Bring up in two stages:**

### Stage 1 — Validation (safe to run alongside DO)

In the Render dashboard, **suspend** the following services before they start:
- `pear-autoreplies-poller`
- `pear-autoreplies-worker`
- `pear-autoreplies-scheduler`

Do **not** yet add the SA Secret File to these three services.

Bring up only:
- `pear-autoreplies-redis` (Key Value — starts automatically)
- `pear-autoreplies-web`
- `pear-autoreplies-harness-poller`

The harness-poller will begin polling the Gmail mailboxes and writing Drafts
rows to the TEST Airtable base (`appmSm1FyerysvtcX`).  Compare its output to
the DO harness over the same time window (see §4 checklist).

### Stage 2 — Cutover (see §5)

Only proceed to Stage 2 after the §4 checklist passes.

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

- [ ] **Harness parity** — The Render `harness-poller` materialises the same
  Drafts rows into the TEST Airtable base (`appmSm1FyerysvtcX`) as the DO
  harness for the same lead window.  Cross-check parse/match/template output
  with `python -m autoreplies.harness stats --since <date>` on both
  environments and `diff` the CSV exports if practical.
  _Result: pending_

- [ ] **Web service reachable** — `/healthz` returns HTTP 200 over HTTPS on
  the Render `.onrender.com` URL.  Confirm TLS is auto-provisioned and the
  certificate is valid.
  _Result: pending_

- [ ] **Pub/Sub push accepted** — POST a well-formed Pub/Sub envelope to
  `/pubsub/inbox` on the Render URL.  Confirm it returns 2xx (the Phase-0
  stub acks all messages).  Confirm that a burst of requests is rate-limited
  (i.e., a 429 appears after `RATELIMIT_PUBSUB_PER_MINUTE` hits in a minute
  from the same IP).
  _Result: pending_

- [ ] **SA domain-wide delegation from Render** — Confirm the harness-poller
  can READ Gmail (it lists messages for at least one monitored mailbox without
  errors in the Render service logs).  Do NOT test SEND from Render until
  cutover — the controlled send test is part of §5.
  _Result: pending_

- [ ] **Redis connectivity** — After un-suspending `-worker` and `-scheduler`
  (with their SA Secret File added), confirm `REDIS_URL` resolves and RQ can
  enqueue/drain a test job.  Check Render logs for connection errors.
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

## 5. Cutover

Only proceed after all §4 checklist items pass.

### Enable the production services

1. Add the SA Secret File (`/etc/pear-autoreply/sa.json`) to
   `pear-autoreplies-poller`, `pear-autoreplies-worker`, and
   `pear-autoreplies-scheduler`.
2. Un-suspend all three services from the dashboard.
3. Run a controlled send test: use `make harness-replay` or a direct
   `python -m autoreplies.harness replay` call to confirm the full pipeline
   (parse → match → template → Gmail SEND) works from Render before flipping
   the Pub/Sub subscription.

### Flip the platform pointers

1. **Pub/Sub push subscription** — update the push endpoint URL in the Google
   Cloud console from the DO URL to the Render URL (either the `.onrender.com`
   address or the custom domain once DNS is set).
2. **`PUBSUB_AUDIENCE`** — update the env var in the `pear-autoreplies-config`
   env group to match the exact URL used in step 1.
   - If validating against `.onrender.com`, set audience to that URL.
   - At custom-domain cutover, set it back to
     `https://autoreplies.pearnyc.com/pubsub/inbox`.
3. **DNS (Google Domains)** — either:
   - Repoint the `autoreplies.pearnyc.com` A/CNAME records to Render, OR
   - Add `autoreplies.pearnyc.com` as a **Custom Domain** in the Render
     `pear-autoreplies-web` service settings (CNAME the domain to the
     service's `.onrender.com` host).  Render will auto-provision a
     Let's Encrypt cert.
4. Keep `PUBSUB_SERVICE_ACCOUNT_EMAIL` intact — the OIDC service-account check
   remains active.

### Suspend the DO poller

SSH to `161.35.13.81` as root:

```bash
cd /opt/pear-autoreplies/app
docker compose stop poller worker scheduler
```

Leave the DO stack otherwise intact for rollback.

---

## 6. Rollback

The DO droplet stays deployable at all times.  To roll back:

1. SSH to `161.35.13.81` as root:
   ```bash
   cd /opt/pear-autoreplies/app
   git pull && docker compose up -d
   ```
2. Repoint the Pub/Sub push subscription endpoint back to the DO URL.
3. Repoint DNS (`autoreplies.pearnyc.com`) back to the DO droplet.
4. Suspend the Render poller/worker/scheduler from the dashboard to avoid
   double-processing.

No data migration is needed: the SQLite cursor files are independent per
platform.  Mind cursor overlap on rollback the same way as cutover — if the
Render poller processed some messages before rollback, those message IDs will
not be in the DO poller's `processed_messages` table and could be re-processed.
If overlap is a concern, copy the relevant `processed_messages` rows from the
Render disk (via `render disk download` or shell access) into the DO SQLite
before restarting the DO poller.

---

## 7. Scheduler tradeoff note

The `pear-autoreplies-scheduler` service runs `workers/scheduler.py`, which
loops forever and renews the Gmail Pub/Sub watch once every 24 hours.  On
Render it is deployed as a **Background Worker** (always-on, `type: worker`).

**Current choice — Background Worker:**
- Identical to DO behaviour: zero code change required.
- Drawback: an always-on instance (billed continuously) for a task that runs
  for a few seconds per day.
- The 24-hour sleep timer resets on every redeploy.  This is harmless: Gmail
  watch subscriptions have a 7-day expiry, and the 24-hour renewal cadence
  leaves a 6-day safety margin.

**Alternative — Render Cron Job (`type: cron`):**
- Cheaper: the container runs only for the duration of the renewal call.
- Fixed wall-clock schedule (e.g., `"0 8 * * *"` — 8 AM UTC daily).
- Requires converting `workers/scheduler.py`'s sleep loop to a one-shot:
  call `_run_once()` (or equivalent) and exit, then declare a
  `type: cron, schedule: "0 8 * * *"` service in `render.yaml`.

**Recommendation:** keep the Background Worker for the launch to minimise
diff.  Revisit the cron conversion post-launch once the Render environment is
stable.
