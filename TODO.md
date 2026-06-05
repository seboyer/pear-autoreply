# TODO

Items deferred for later — not blocking the current phase of work but worth coming back to.

## In flight

- **PR #8 — apartment matcher overhaul + Drafts.Sender + template-lookup fix** ([github](https://github.com/seboyer/pear-autoreply/pull/8), branch `claude/intelligent-kalam-a07823`). Implementation merged; post-deploy verification still pending. After the harness has been running against new leads for a few hours, re-pull TEST Drafts via Airtable MCP and confirm:
  - `Apartment Match Strategy = "address"` for ≥ 65% of new rows (bucketing target was 72%)
  - `Template Source = "agent"` for monitored mailboxes (was always `"pear_default"` due to the field-mismatch bug — see commit on PR #8)
  - `Sender` populated on every row with the polled mailbox

  Re-bucketing harness lives at `~/notes/autoreplies/` (`bucket_inquiries.py` + `inquiries.json` + `apts.tsv`) — re-runnable against a fresh MCP pull.

## Operational caveats

- **First run of `harness diff`**: the prod-side fetch is scoped to `Gmail Message ID (Autoreply) != ""`, which loads every autoreply-tagged Inquiry since that field was added in prod. On the first invocation, use a `--since` very close to today so the result set stays small. Widen the window only after confirming the diff output shape is sane.

- Ensure that all airtable resources are identified by ID not name
