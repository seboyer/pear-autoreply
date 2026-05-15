# Pear NYC — Rental Platform Lead Autoreply System

**Status:** draft for approval — *2026-04-25*
**Owner:** Sam
**Replaces:** Zapier-based StreetEasy/Zillow lead handling

---

## TL;DR

A Python service that ingests StreetEasy / Zillow lead emails in real time via Gmail Pub/Sub, identifies the receiving agent by recipient address, parses the inquiry, sends a controlled auto-reply using each agent's per-record template (filled by Claude Haiku 4.5), best-effort matches the apartment for analytics, writes a row to the Airtable `Inquiries` table, persists canonical data to Supabase, and posts a notification to a single shared Slack channel. Runs on a DigitalOcean droplet with Redis-backed worker queue. Targets sub-30-second time-to-reply at 500+/day.

---

## Decisions locked in

| Decision | Choice | Why |
|---|---|---|
| Email ingestion | Gmail API + Pub/Sub push (`users.watch`) | Real-time, robust, scales |
| Reply mode | Auto-send | Sam confirmed; speed > review-loop |
| Volume target | 500+/day | Justifies a queue + workers |
| Supabase write | Rewritten in-repo (existing script as reference) | Single deployable, single set of credentials |
| Reply template | Per-agent stock template stored in `Users.Autoreply (Agent)` (legacy field, repurposed). Generic Pear-wide fallback when an agent's field is empty | Agents own their voice; fallback prevents gaps |
| AI model | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) | Cheap, fast, best instruction-following for slot-filling. Gemini gives no advantage just because Gmail is Google |
| Apartment match | URL/listing-ID first, address fallback — **for analytics only**, never gates the reply | Sam confirmed |
| User matching | Match existing users by email/phone only — **never create a new user from a lead** | Sam confirmed |
| Slack target | `#platform-leads` (private channel), new Slack app | Sam confirmed |
| Reply destination | Always thread; use the `Reply-To:` header from the platform payload (StreetEasy/Zillow set it correctly to route back to the prospect) | Sam confirmed |
| Reply timing | Hold during quiet hours 23:00–07:00 (NY time) and release at 07:00. Add 2–5 min random jitter to all sends | Feels human, less obviously automated |
| Sender address | Agent's primary `firstname@pearnyc.com` mailbox (not the legacy `Autoreply Email (Agent)` field) | Sam confirmed |
| Sources | StreetEasy (`noreply@email.streeteasy.com`) + Zillow network (`rentalclientservices@zillowrentals.com`, covers Zillow/Trulia/HotPads) | Confirmed by mbox analysis: 99.7% of 62k historical leads |
| Canonical IDs | All inquiries flow Airtable → Supabase. Airtable record ID is the Supabase primary key (`id`). `user_id` and `apartment_id` in Supabase are likewise Airtable record IDs. Never write to Supabase before Airtable. | Sam confirmed; matches existing Zapier-script schema |
| Dev environment | Staging copy of the Pear Tracker base | Sam confirmed |
| Failure handling | Always send the reply (it doesn't depend on apartment match). If agent isn't found or prospect email is missing, skip the send and Slack-alert — never send an unattributed reply | Reply quality doesn't depend on the analytics layer |

---

## AI model recommendation: Claude Haiku 4.5

Recommended: `claude-haiku-4-5-20251001`.

For a slot-filling task against a tightly-defined template, Haiku 4.5 is the right pick:

- **Cheap.** At ~500 leads/day with ~2k tokens per call (template + parsed fields + system prompt), that's ~1M tokens/day on Haiku, which is roughly **$1–3/day** all-in.
- **Fast.** Sub-second p50 latency, which keeps end-to-end time-to-reply well under 30 seconds.
- **Tightly steerable.** When the prompt contains the agent's verbatim template and an instruction to *only* fill the bracketed slots, Haiku 4.5 follows that constraint reliably. Use Anthropic's structured tool-use to force a JSON output containing the filled fields and the final body — never let the model freeform the reply email.
- **Provider-agnostic exit.** The reply generator is a small, swappable component. If we ever want to A/B against GPT-4.1-mini or move to a fine-tuned local model, it's a one-file change.

**Safety rails on top of the model:**

1. The Airtable template is the source of truth — the model is *assembling*, not authoring.
2. A post-generation regex check rejects any reply that contains placeholder syntax (`{{...}}`, `[…]`), URLs the template didn't contain, or that exceeds a length cap. Reject → fall back to a strictly literal slot-fill (no LLM) and send that.
3. Every reply is logged with the input fields, model output, final sent body, and message-id, so any drift is auditable.

---

## End-to-end flow

```
StreetEasy/Zillow email
        │
        ▼
Gmail (Workspace, agent@pearnyc.com)
        │   Pub/Sub watch on the inbox
        ▼
Cloud Pub/Sub  ──push──►  FastAPI /pubsub/inbox
                                  │
                                  ├─ Verify JWT, dedupe by historyId
                                  ├─ Fetch full message via Gmail API
                                  └─ Enqueue job → Redis (RQ)
                                                │
                                                ▼
                                         Worker pipeline
                                                │
        ┌───────────────────────────────────────┼───────────────────────────────────────┐
        ▼                                       ▼                                       ▼
  Identify agent                          Parse inquiry                          Compose reply
  by `To:` header                         (regex + LLM fallback)                 (template + Haiku 4.5)
        │                                       │                                       │
        └───────────────┬───────────────────────┘                                       │
                        ▼                                                               ▼
                Apartment match (URL → address)                                  Send via Gmail API
                (best-effort, analytics only)                                  (impersonate agent)
                        │                                                               │
                        ▼                                                               │
                Existing-user lookup (email/phone)                                      │
                        │                                                               │
                        ├──────────────────────┬───────────────────────────────────┐    │
                        ▼                      ▼                                   ▼    ▼
                  Airtable Inquiries     Supabase write                    Slack notification
                  (processing layer)     (canonical)                       (#leads, tag agent)
```

---

## Component breakdown

### 1. Ingestion — Gmail filter + Pub/Sub watch (combined)

The cleanest way to ingest exactly the leads we want, and nothing else, is to combine two Google-native primitives. The spec below is **empirically derived** from analysis of 62,470 historical lead emails (Jan 2024 – Apr 2026):

**(a) Workspace-wide Gmail filter rule** that auto-applies a `Pear/Leads` label to any incoming message matching:

- `From:` is in this allowlist (covers 99.7% of historical leads):
  - `noreply@email.streeteasy.com` — StreetEasy lead inquiries (32,649 messages observed)
  - `rentalclientservices@zillowrentals.com` — Zillow / Trulia / HotPads lead inquiries (29,382 messages — single sender for all three)
- AND `Subject:` matches one of these regexes (canonical phrases observed verbatim across the entire dataset):
  - StreetEasy: `^.+ StreetEasy Inquiry From .+$` (address prefix + canonical phrase + prospect name)
  - Zillow: `^New Zillow Group Rentals Contact:.+$`
- AND `Subject:` does **not** match the StreetEasy transactional patterns observed in the data (account-security alerts come from the same StreetEasy sender and would otherwise sneak in): `Do Not Share: Your StreetEasy Code`, `Magic Code`, plus other security/account alerts. Easiest implementation is an explicit deny-regex: `(Do Not Share|Magic Code|account|security)` in subject → exclude.
- AND the message is *not* a thread reply (no `In-Reply-To` header). Mbox showed only 0.27% of leads have `In-Reply-To`, so this filter is cheap insurance against any edge case.

**(b) Per-agent `users.watch`** with `labelIds: ["Pear/Leads"]` and `labelFilterAction: "include"`. Pub/Sub fires *only* when an email lands and the filter labels it. We never get notified about ordinary mail or marketing.

Why both? The filter is fast, native, and free. The watch turns labeled mail into a real-time event. Either alone is worse: filter without watch means polling; watch without filter means we wake up for every email and have to discard.

**Service account.** One service account in the existing GCP project with **domain-wide delegation** for `pearnyc.com`. Scopes: `gmail.modify` (read + send + label) and `gmail.settings.basic` (manage filters). Sam (workspace admin) grants delegation in the Admin Console.

**The webhook.** A FastAPI endpoint (`POST /pubsub/inbox`) accepts Pub/Sub push:
- Verifies the OIDC JWT signature and audience.
- Extracts `emailAddress` and `historyId`.
- Looks up the previous historyId for that mailbox in Redis, calls `users.history.list` (with `labelId: Pear/Leads`, `historyTypes: ["messageAdded"]`) to get only newly-labeled messages.
- For each new message: enqueue a job keyed on the Gmail message-id (idempotency).
- Acks the Pub/Sub message immediately. All real work happens off the request path.

**Defense in depth.** Even after the filter, the worker re-validates: sender domain in allowlist, subject matches lead patterns, message has `Reply-To` set, body parses to at least name+email. Anything failing that check is Slack-alerted, not sent.

### 2. Worker pipeline (RQ)

Single end-to-end job per message-id, but written so each persistent side-effect can be skipped on retry if it already succeeded. The Airtable record ID — issued by the Inquiries insert — is the canonical join key from that point forward; nothing downstream creates IDs. Pseudocode:

```
def process_lead(message_id, mailbox_email):
    state = load_state(message_id)               # Redis-backed
    if state.fully_done: return

    if not state.airtable_record_id:
        msg = gmail.get_message(mailbox_email, message_id)
        agent = airtable.find_monitored_user_by_primary_email(mailbox_email)
        if not agent: alert_slack_and_quarantine(...); return

        parsed = parse_lead_email(msg)
        reply_body = compose_reply(agent, parsed)
        sent_id = gmail.send_reply(mailbox_email, msg, reply_body)   # auto-send, jittered + quiet-hours-aware
        state.reply_sent_message_id = sent_id                        # tracked in JobState; not persisted to Airtable

        apartment = match_apartment(parsed)                          # best-effort, may be None
        user      = match_existing_user(parsed)                      # match-only, never create
        record_id = airtable.create_inquiry(                         # ← AIRTABLE ISSUES THE ID
            parsed=parsed,
            apartment_record_id=apartment["id"] if apartment else None,
            user_record_id=user["id"] if user else None,
            gmail_message_id=message_id,                             # durable backstop
        )                                                            # Agent is NOT a parameter — it's a lookup
        state.airtable_record_id = record_id                         # via the linked Apartment.
        state.parsed = parsed                                        # cache for downstream steps
        save_state(state)

    if not state.supabase_done:
        supabase.upsert_inquiry(                                     # ← Airtable record ID is the PK
            id=state.airtable_record_id,
            user_id=state.parsed.user_record_id,                     # Airtable record ID, or None
            apartment_id=state.parsed.apartment_record_id,           # Airtable record ID, or None
            **fields_from(state.parsed),
        )
        state.supabase_done = True
        save_state(state)

    if not state.slack_done:
        slack.post_lead(state)
        state.slack_done = True

    state.fully_done = True
    save_state(state)
```

Each external call is wrapped in retry-with-backoff. If the reply-send step fails, we still write the analytics rows with `reply_status = failed` and Slack-alert. If a step downstream of Airtable fails, retries pick up from there using `state.airtable_record_id` — Airtable insert is never re-attempted, so we don't create orphan rows.

### 3. Email parser

Two-stage, with measured success rates from the 200-message sampling pass against the mbox:

1. **Source-specific regex.** Dispatch on `From:` to one of two parsers:

   **StreetEasy parser** (regex against the HTML body's embedded `<meta>` tags + subject line):
   - `first_name`: 96% extracted
   - `email` (prospect): 100%
   - `phone`: 85%
   - `apartment_address`: 95.5%
   - `listing_url`: 100%

   **Zillow parser** (regex against the canonical subject line + HTML body — Zillow has no `text/plain` MIME part, so the parser must extract from the HTML alternative):
   - `first_name`: high — current Zillow format includes the prospect's first name in the body (`<First Last> says: …`). Confirmed empirically across the legacy mailbox (Aug 2024–Apr 2026). Earlier "0%" claim was an artifact of attempting plain-text extraction on an HTML-only body.
   - `email` (prospect): 100% (taken from `Reply-To` header)
   - `phone`: high — current Zillow format includes phone in the body. Same provenance as `first_name`.
   - `apartment_address`: 100% (parsed from the subject prefix)
   - `listing_url`: 100%

   Implication: Zillow leads usually carry the same fields as StreetEasy. The reply template's `{{first_name|there}}` fallback is still essential because the format may drift back, and the Slack alert + Airtable record must still gracefully render when either field is missing. Don't gate any logic on `first_name` or `phone` presence for Zillow.

2. **LLM fallback.** Only triggers when the regex parser misses fields it *should* have extracted (e.g., StreetEasy with no first_name). For Zillow, do not invoke the LLM trying to recover fields that aren't in the email — it'll just hallucinate. Call Haiku 4.5 with the email body and a JSON tool schema. Tag the lead with `parser_used = llm_fallback` so we can monitor drift in source email formats.

The full raw email (headers + body, MIME-intact) is stored in Supabase regardless, so anything we miss can be re-parsed offline.

### 4. Reply generator

The legacy `Autoreply (Agent)` field is currently a **stock message with no variables** — fine, but a missed opportunity, since we now have the prospect's name and the listing address to personalize on. I'd recommend giving every agent a template like:

```
Hi {{first_name|there}},

Thanks for reaching out about {{apartment_address|the listing}}! I'd love to help you find a great place. Are you available for a showing this week?

Talk soon,
```

Pipe-delimited fallbacks (`{{first_name|there}}`) handle missing slots gracefully. We can ship a default like this and let agents customize their own.

**Signatures: use the agent's Gmail default, do not inline.** The template body intentionally ends with the closing line and no name. The system appends the agent's Gmail signature at send time, fetched from `users.settings.sendAs.get` (the entry where `isDefault = true`, or the entry matching the agent's primary mailbox). Why:

- Agents already maintain their signatures in Gmail (phone, license number, photo, social links, brokerage disclosures, etc.). Pulling that data into Airtable to re-render in our template would duplicate what's already authoritative in Gmail.
- Reusing the Gmail signature keeps the autoreply visually identical to other replies the agent sends from web/mobile, so prospects don't see a stylistic discontinuity.
- Updates to a signature happen in one place. No deploy or template edit needed when an agent updates their phone number.

Implementation: cache each agent's signature HTML in Redis with a 24h TTL, refresh on miss or on `/admin/reload-signatures`. Signatures are HTML, so the reply is sent as `multipart/alternative` — plain-text part contains the template body only, HTML part contains the template body (formatted as HTML) plus the signature appended below the closing line.

**Generic Pear-wide fallback.** Stored as a constant in code (or a single Airtable record on a `Settings` table). Used when an agent's `Autoreply (Agent)` field is empty.

**Generation:**

```
template_text = airtable.get(agent_record, "Autoreply (Agent)") or PEAR_FALLBACK_TEMPLATE
slots = {
    "first_name": parsed.first_name or None,            # → fallback to "there"
    "apartment_address": parsed.apartment_address or None,
}
# agent_name and agent_phone are NOT slots — they live in the Gmail signature
```

Haiku 4.5 is called with a system prompt that says, in essence: "Here is an email template with `{{slot}}` or `{{slot|default}}` placeholders. Here are the values to use. Produce a JSON object `{filled_subject, filled_body}` by substituting verbatim. Do not change wording outside of slots. Apply the `|default` fallback when a slot value is null." Output is forced via tool-use schema — the model can't return free text.

A safety post-check verifies no `{{` remains, no new URLs were introduced, and the filled body length is within ±20% of the **literal-fill** length (i.e. of what a pure regex substitution on the same template + slots would have produced). Comparing against the raw template length is wrong in practice — slot syntax like `{{apartment_address|the listing}}` legitimately shrinks ~30% when the slot has a short concrete value. On failure → literal Python regex fill, no LLM.

**Quiet hours + jitter.** The reply isn't sent immediately; it's enqueued with a `send_after` timestamp:
- Base delay: random 2–5 min from inquiry receipt (humanizes timing).
- Quiet hours: if `send_after` falls between 23:00 and 07:00 New York time, push to 07:00 + jitter the next morning.
- A `send-due` worker polls for due messages each minute and dispatches via Gmail API.

**Reply destination (important).** The original email lands from `noreply@streeteasy.com` or `noreply@zillow.com`, but per Sam these emails always carry a `Reply-To:` header that routes back to the prospect. The reply destination is chosen in this order:

1. **Default — thread reply.** Use Gmail's thread mechanics with the original `threadId`, set `In-Reply-To` and `References` headers, and direct the message to the address in `Reply-To:`. The prospect sees a clean continuation.
2. **Defensive fallback.** If `Reply-To:` is unexpectedly missing or matches a denylist of known platform-relay domains, fall back to a fresh email to `parsed.email` with subject `Re: <listing address> — <agent name>`.
3. **Skip.** If both the `Reply-To:` and `parsed.email` are missing/invalid, do not auto-send. Slack-alert with the raw email so an agent can reach out manually. Still write the analytics row (`reply_route = skipped`).

The decision is logged on the inquiry record (`reply_route = thread | direct | skipped`).

### 5. Airtable client

- **Schema management.** All Airtable tables and fields are referenced by *immutable IDs* (`tbl…`, `fld…`), never by display name. The curated set of IDs lives in `src/autoreplies/services/airtable_schema.py`, generated from the Airtable metadata API by `scripts/generate_airtable_schema.py`. The CURATED dict at the top of that script is the project's contract — a field is in the schema module iff it's named there. To add a field, edit CURATED, regenerate, and plumb it through. Never edit `airtable_schema.py` by hand. Only base IDs live in `.env` / `Settings`; everything else (table IDs, field IDs, view IDs) lives in the schema module.
- Look up agent: `Users` table, filter `Autoreply Enabled (Agent) = TRUE` AND `Email = <recipient mailbox>` (the user's primary `firstname@pearnyc.com`). Cache per-agent record for the worker lifetime.
- Match apartment (analytics-only, non-blocking). The strategy is source-dependent because — confirmed empirically — StreetEasy and Zillow listing identifiers are *not* linked in either email. Pear posts only to StreetEasy and Zillow syndicates from there, but Zillow's emails carry only opaque redirect codes (`zillow.com/r/<alphanumeric>`), never a StreetEasy listing ID. So:
  1. **StreetEasy leads — URL-first.** Extract the StreetEasy numeric listing ID from `parsed.listing_url` (matches `streeteasy.com/rental/<id>`), then find `Apartments` where the `Streeteasy` URL field contains that same ID. Highest-confidence match.
  2. **StreetEasy leads — address fallback.** If no listing-ID match, fall through to the address-fuzzy-match path below.
  3. **Zillow leads — address-only.** Zillow URLs cannot be reverse-mapped to a StreetEasy listing, but Zillow emails *do* include the full structured address with **unit number + borough** (e.g. `170 Prospect Pl #3B, Brooklyn`). Normalize and fuzzy-match (rapidfuzz `WRatio >= 92`) against `Apartments.Full Address`. The unit + borough makes this more accurate than a bare street-name match — multi-unit buildings stop being ambiguous.
  4. **Address normalization** (shared by both sources): lowercase, strip whitespace, expand/canonicalize street suffixes (St → Street, Ave → Avenue, Pl → Place), normalize unit prefixes (`#3B`, `Apt 3B`, `Unit 3B` → `3B`), expand borough abbreviations (BK → Brooklyn).
  5. No match → `apartment_id = null`, `match_confidence = none`. The reply still sends; only the analytics row is sparse.
- **Match existing user only — never create.** `Users` table, filter `Type != "Agent"` AND `Type != "Admin"` AND (`Email = parsed.email` OR `Phone = parsed.phone`). On hit, link the inquiry to that user. On miss, leave the `User` field empty (the inquiry still gets created — the prospect just isn't yet a user record). Sam confirmed this rule: lead inquiries are not authoritative enough to mint user records.
- Create `Inquiries` row: `Name (Form)`, `Email (Form)`, `Phone`, `Message`, link `Apartment` (if matched), link `User` (if matched), `Method = "Web"`, `Type (Non Website) = "StreetEasy"` or `"Zillow"`. `Date Created` auto. **Returned record ID is the canonical join key for everything downstream.**
  - `Method` is `"Web"` (not `"Email"`) — the field describes the prospect's contact channel (the public rental platform), not how the lead reached us.
  - `Agent` on Inquiries is a **lookup field** through the linked `Apartment`, not a writable link. Don't pass it to `create_inquiry`. When apartment matching fails, the Agent lookup is null on the analytics row — that's accepted; live agent attribution lives on the Slack notification.
  - The Gmail message-id of the *sent reply* is **not** persisted to Airtable. It's tracked in `JobState.reply_sent_message_id` for in-pipeline use. If we later need durable storage, add a curated field and plumb it through.
- **All formula construction goes through `pyairtable.formulas`** (`AND`, `OR`, `EQ`, `NE`, `FIND`, `Field`). String interpolation into `filterByFormula` is forbidden — values from parsed lead emails are attacker-shaped and will break or inject into raw f-string formulas.
- **New field already added to production:** `Gmail Message ID (Autoreply)` (single-line text) on the Inquiries table. Populated on insert. Two purposes: (a) durable backstop for idempotency lookups if Redis loses state — `find_inquiry_by_gmail_message_id` resolves a Gmail message-id back to its Airtable record ID; (b) audit trail tying every Airtable row back to the source email.
- Rate-limit: Airtable's 5 req/sec/base. Use a process-wide token bucket; bursts of leads queue naturally.

### 6. Supabase writer

Rewrites the existing Zapier script (kept for reference in `legacy/zapier_supabase_post.py`) into a `supabase_client.py` module exposing `upsert_inquiry(id, **fields) -> dict`. Logic from the existing script we keep:

- Same target table: `https://fuacxndojzybijrqdbym.supabase.co/rest/v1/inquiries`.
- Same `Prefer: resolution=merge-duplicates,return=representation` header for upsert-on-`id` semantics.
- Same coercion helpers (`to_null`, `to_number_or_null`, `to_date_or_null`) — keep them as `utils/coerce.py`.
- Same null-stripping before POST.
- Same field shape and naming. The new pipeline produces a strict subset of the fields the existing script supports.

**Critical change in ID semantics:**

- `id` is the **Airtable record ID** returned from the Inquiries insert in §5 — *not* the Gmail message-id. This matches the existing Supabase schema, where `id`, `user_id`, and `apartment_id` are all Airtable record IDs (e.g. `recXXXXXXXXXXXXXX`) and serve as join keys back to Airtable.
- `user_id` is the matched Airtable User record ID, or null when no existing user matched.
- `apartment_id` is the matched Airtable Apartment record ID, or null on no-match. `apartment_failsafe` carries the parsed address regardless.
- `gmail_message_id` is also written on the Supabase row (see column spec below) for direct auditability — but it is *not* the primary key; `id` (the Airtable record ID) is.

**New Supabase column to add** (Sam to provision before launch):

- Column name: `gmail_message_id`
- Type: `text`
- Nullable: yes (legacy Zapier rows have no Gmail message-id)
- Indexed: yes (so we can dedup or trace back from a message-id quickly)
- Mirrors the Airtable `Gmail Message ID (Autoreply)` field one-for-one for every row this pipeline writes.

**Other field semantics for rental-platform leads:**

- The script currently expects fields populated by Zapier from the pearnyc.com form (e.g. `credit`, `income`, `budget`, `move_in_date`, `guarantor`). Those are not present on rental-platform leads — we leave them null and the null-strip in the existing script keeps them out of the payload. No schema change needed.
- Populated by this pipeline: `id` (Airtable Inquiries record ID), `gmail_message_id` (Gmail message-id), `name_form`, `email_form`, `phone`, `message`, `user_id` (Airtable User record ID, or null), `apartment_id` (Airtable Apartment record ID, or null), `apartment_failsafe` (raw parsed address string), `email` and `name` (mirror of form fields, kept for compatibility with downstream queries), `type_platform` (`"StreetEasy"` or `"Zillow"`), `method` (`"Web"` — mirrors the Airtable Inquiries `Method` value), `date_created` (ISO 8601 from Gmail's internalDate), `sales` (always `false`).
- Use `supabase-py` instead of raw `requests` for cleaner error handling and connection reuse. Equivalent semantics, idiomatic Python.
- Upsert-on-`id` semantics still matter: if a retry hits Supabase a second time with the same Airtable record ID, the merge-duplicates header makes it a no-op rather than a constraint violation.

### 7. Slack notifier

New Slack app (`pear-autoreply` or similar), bot scope: `chat:write` only. Posts to `#platform-leads` (private). The bot needs to be invited to that channel once.

This feed is for **admin visibility, not agent notifications** — so we do not `@`-mention the agent. The agent is identified by name + email in the message body, which is sufficient for an admin scanning the channel to see who's covering the lead.

Block Kit message includes the six fields Sam asked for — name, email, phone, address, message, agent — plus apartment-match status as a small badge:

```
🏠 New StreetEasy lead
   Agent: Jane Doe · jane@pearnyc.com
   Prospect: Casey Prospect · casey@example.com · (646) 555-0123
   Listing: 123 Main St #4B  ·  matched 92% to "123 Main St" in Airtable
   ↳ "Hi! I'd love to see this place this weekend..."
   View Inquiry in Airtable · View thread in Gmail
```

For Zillow leads where first_name and phone are absent, those lines show as `Prospect: casey@example.com (no name on file)` — never blank. When the apartment isn't matched, the badge reads `no apartment match` and the parsed address is shown as plain text so an admin can manually link it.

---

## Failure modes & idempotency

| Scenario | Behavior |
|---|---|
| Pub/Sub redelivers the same notification | History-id Redis check → no-op |
| Same Gmail message-id processed twice | Redis state lookup short-circuits at the right phase. If Redis was wiped, fall back to filtering Airtable by `Gmail Message ID (Autoreply) = <id>` to recover the existing record before re-writing |
| Airtable insert succeeds, Supabase write fails | Retry resumes from Supabase using the Airtable record ID from Redis — Airtable insert is *not* re-attempted, so no orphan rows |
| Airtable insert fails | Job fails entirely; nothing written to Supabase or Slack. Standard backoff + dead-letter |
| Gmail send fails (transient) | Retry 3× with backoff; on permanent fail, alert Slack + still write analytics row marked `reply_status = failed` |
| Haiku call fails / returns invalid output | Fall back to literal Python format-fill; record `reply_method = template_only` |
| Agent not found for recipient | **Do not send**; Slack-alert with the raw email; flag for ops |
| Apartment not matched | Send reply normally; analytics row has `apartment = null` |
| User not found by email/phone | Leave the `User` link empty on the Inquiry; reply still sends and the analytics row records the inquiry without a User link. Per the locked-in decision, never mint a user record from a lead. |
| Airtable rate-limit hit | Exponential backoff in the rate-limiter; jobs simply wait their turn |
| Anthropic 429 / 5xx | Retry with backoff; on giveup, fall back to literal template fill |
| Watch expired (>7 days) | Daily cron renews `users.watch` for all agent mailboxes |
| Agent's Gmail signature fetch fails | Send the reply with no signature (still better than skipping); Slack-warn so the agent's signature can be re-fetched manually |
| Agent has no default signature configured in Gmail | Send without; Slack-warn so the agent can set one |
| Prospect email missing or invalid | Don't auto-send, Slack-alert, still write analytics row with `reply_route = skipped` |
| Reply bounces (hard fail) | Monitor agent mailbox for `MAILER-DAEMON` and Slack-alert; mark inquiry `reply_status = bounced` |
| Agent's `Autoreply (Agent)` field is empty | Use the generic Pear-wide fallback template; mark `template_source = pear_default` |
| Send timestamp falls in quiet hours (23:00–07:00 NY) | Hold and release at 07:00 + jitter |
| Worker dies mid-pipeline | Job retried by RQ; idempotency keys (Gmail msg-id, Supabase `id` upsert) prevent dupes |

A "lead failed" Slack alert fires whenever any required step (find agent, send reply) breaks, with a link to the raw Gmail message and the worker logs.

---

## Security & secrets

- **GCP service account JSON** with domain-wide delegation. Stored in `/etc/pear-autoreply/sa.json` (root-owned, 600).
- **Anthropic API key.**
- **Airtable PAT** (scoped to Pear Tracker only).
- **Supabase service-role key.**
- **Slack bot token** (scope: `chat:write`).
- **Admin endpoint bearer token** — long random secret (e.g. 256-bit `secrets.token_urlsafe`). See "Admin endpoint auth" below.
- All loaded via env vars; in production use systemd `EnvironmentFile=` or Docker secrets. `.env.example` checked into the repo, real `.env` never.
- HTTPS for the Pub/Sub endpoint via Caddy or nginx + Let's Encrypt — required by Pub/Sub push. Caddy auto-provisions and renews certs with no extra config.

**Admin endpoint auth.** `/admin/replay/{message_id}`, `/admin/quarantine`, `/admin/healthz/detail`, etc. need protection. Recommended approach, optimizing for security + simplicity + no-extra-services:

- Single bearer-token check via FastAPI dependency: requests must include `Authorization: Bearer <ADMIN_TOKEN>`. Constant-time compare (`secrets.compare_digest`) to defeat timing attacks.
- Token generated once with `python -c "import secrets; print(secrets.token_urlsafe(32))"`, stored in the env file alongside other secrets.
- All admin requests logged with the originating IP and a request-id. Rate-limited (e.g. 30/min) at the Caddy layer to blunt brute-force.
- Token rotation: trivial — change the env var and restart. Plan to rotate quarterly.

This avoids Cloudflare Access / Tailscale / OAuth flows. If we later decide we want SSO, FastAPI middleware can be swapped without touching the rest of the system. The `/pubsub/inbox` endpoint is separately protected by JWT signature verification on the Pub/Sub push, so it doesn't need the bearer token.

---

## Deployment — DigitalOcean droplet

For 500+/day, the droplet path beats Render once you factor in always-on workers + Redis + scheduled tasks. Render's free/$7 tier can't run dedicated workers and the cold-start risk on the Pub/Sub webhook is unacceptable.

**Stack:**
- DigitalOcean droplet, 2 vCPU / 4 GB RAM (`s-2vcpu-4gb`, ~$24/mo).
- Ubuntu 24.04 + Docker Compose.
- Containers:
  - `web` — FastAPI behind Caddy, exposes `/pubsub/inbox`, `/healthz`, `/admin/replay/{message_id}`.
  - `worker` — RQ worker × 2.
  - `scheduler` — daily watch-renewal cron.
  - `redis` — local, persistent volume.
- Logs to a single JSON file rotated by Docker's `json-file` driver, shipped to Better Stack (or whatever Sam already uses).
- Backups: droplet snapshot weekly + Supabase is the canonical store anyway.

**Scale-up trigger** — when sustained throughput exceeds ~5 leads/sec or worker queue depth p95 > 60s for an hour: bump to a 4-vCPU droplet and run 4 workers, or split workers onto a second droplet pointing at the same Redis.

**Cost estimate:**

| Item | Monthly |
|---|---|
| DO droplet | ~$24 |
| Anthropic Haiku 4.5 (~500/day × 2k tok) | ~$30–90 |
| Domain + DNS + Caddy TLS | $0 (already have pearnyc.com) |
| GCP Pub/Sub | <$1 (well under free tier at this volume) |
| **Total** | **~$55–115 / mo** |

---

## Implementation phases

1. **Phase 0 — scaffolding (½ day).** Repo layout, `.env`, Docker Compose, Caddy, healthcheck, CI lint/test.
2. **Phase 1 — Gmail ingestion (1–2 days).** Service account, domain-wide delegation, Pub/Sub topic + subscription, watch-renewal cron, `/pubsub/inbox` end-to-end including JWT verify and history fetch. Verify with one agent's mailbox first.
3. **Phase 2 — Parser + reply (1–2 days).** StreetEasy + Zillow parsers; Haiku 4.5 reply generator with safety post-check; literal-fill fallback. Unit tests with real-world fixtures.
4. **Phase 3 — Airtable + Supabase + Slack (1 day).** Airtable client, supabase rewrite, Slack notifier, end-to-end happy path on a single agent in a staging Airtable copy.
5. **Phase 4 — Hardening (1 day).** Idempotency, retries, rate limiting, dead-letter, observability, replay endpoint.
6. **Phase 5 — Validation soak (1–2 weeks).** Before Phase 6 cutover, the testing harness (see [`TESTING_HARNESS_PLAN.md`](./TESTING_HARNESS_PLAN.md), build instructions in [`HARNESS_BUILD_BRIEF.md`](./HARNESS_BUILD_BRIEF.md)) runs in parallel against the same Gmail mailboxes, materializing every would-have-sent reply as a Drafts row in a test Airtable base. Sam + sales team review until they agree the system is ready. The harness reuses production parsers/matchers/template-fill via the strategy pattern; production never imports the harness. Zapier keeps handling traffic during this window.
7. **Phase 6 — Cutover (½ day).** Roll out one agent at a time. Disable the Zapier path per agent as we go. Monitor for a week before declaring done.

Total: ~6–8 working days for a single dev, plus a 1–2 week validation soak in parallel.

---

## Empirical findings from mbox analysis (2026-04-25)

Analyzed Sam's 2.1GB legacy autoreply mailbox: 62,470 messages over 831 days (avg ~75/day on this single mailbox; production volume across all agent mailboxes scales accordingly). All filter and parser specs above are derived from this dataset rather than guesses. Key facts:

- Two senders cover **99.7%** of historical leads — `noreply@email.streeteasy.com` and `rentalclientservices@zillowrentals.com`. No other lead-generation domain (Apartments.com, RentHop, etc.) appears in the data, confirming we can scope to these two.
- Subject patterns are **stable verbatim** across 2+ years — no detected drift in the canonical phrases.
- `Reply-To` coverage is **99.7%** overall, **100%** on actual lead messages from the two confirmed senders. Top destination domains: gmail.com (78%), yahoo.com (6%), icloud.com (4%) — all real prospect inboxes, confirming Sam's claim that Reply-To routes correctly.
- StreetEasy emails contain rich structured metadata (HTML `<meta>` tags) that makes regex extraction trivial. Zillow emails are sparser — only address + listing URL + email + message body; **no first name, no phone**. This is a Zillow product limitation, not a parser shortcoming.
- StreetEasy and Zillow listing IDs are *not* cross-referenced in the emails. Zillow's URL is an opaque tracking redirect (`zillow.com/r/<alphanumeric>`). Of 500+ sampled Zillow emails, zero contained a StreetEasy reference. Zillow leads must therefore be apartment-matched by address rather than ID — but Zillow's address format includes unit + borough, so the address path is reliable.
- Negligible noise: 0.04% of messages have attachments, 0.27% are thread replies, encoding is clean throughout. No surprise edge cases to plan for.

A small set of anonymized fixture emails (3 StreetEasy + 3 Zillow) is staged in the outputs folder for use in unit tests.

## Remaining open items

All resolved as of 2026-04-25:

| Item | Resolution |
|---|---|
| Fallback template | Drafted — see appendix A. Saved to repo as `FALLBACK_TEMPLATE.md` for Sam to edit. |
| `sales` Supabase column | Always set to `false` for rental-platform leads. Sales leads are out of scope. |
| Slack agent identification | Show name + email in the message body; don't `@`-mention. Feed is for admin visibility, not agent pings. |
| Admin endpoint auth | HTTP bearer token with constant-time compare. Rate-limited at Caddy. No third-party service. |
| StreetEasy ~4% no-first-name leads | Use the template's `{{first_name|there}}` fallback. No LLM extraction — cleaner and Hi-there-style salutations read fine. |
| Monitored set | Users with `Autoreply Enabled (Agent)` checked (spans some Agents and Admins) | Single Airtable source of truth; allows per-user opt-in without overloading `Type` |

---

## What this plan deliberately does *not* cover

- Sources 1 (pearnyc.com platform) and 3 (Quo/OpenPhone). Out of scope per Sam's brief.
- Sales leads (`sales = true` in Supabase) — different workflow, currently out of scope. May be folded in later.
- Re-architecting Airtable. Inquiries remains a processing layer; Supabase remains canonical.
- Reply *content* changes. Each agent's existing template is the source of truth — we're assembling, not rewriting.
- Migration of historical Zapier-handled leads. New system handles new leads only.

---

## Appendix A — Generic Pear-wide fallback template

Saved separately as `FALLBACK_TEMPLATE.md` in this folder so Sam can edit without touching the plan. **Sam noted this draft will need revision once he coordinates with sales on qualification questions; treat it as a placeholder pending that pass.**

```
Hi {{first_name|there}},

Thanks for your interest in {{apartment_address|the listing}}! I'd love to help you find a great fit in NYC.

Are you available for a showing this week? If you can share a few day/time options, I'll get something on the calendar. In the meantime, feel free to reply with any questions about the apartment, the neighborhood, or our application process.

Talk soon,
```

The agent's Gmail signature is appended automatically at send time, so the template body deliberately ends at "Talk soon," with no name or contact info. Renders cleanly for both Zillow leads (no first_name available) and StreetEasy leads (typically full data). Used only when an agent's own `Autoreply (Agent)` field is empty.
