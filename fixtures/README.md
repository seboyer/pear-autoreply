# Fixtures

Email fixtures used in unit tests for the parsers and the pipeline.

## Layout

- `fixtures/anonymized/streeteasy/` — checked-in StreetEasy `.eml` files. Two variant labels in the filename:
  - `tour__<address>__<key>.eml` — body contains "Has Requested a Tour for …" + "Renter's Preferred Tour: …" (~84% of SE leads in the legacy mailbox).
  - `question__<address>__<key>.eml` — body contains "You Received a Question About …" followed by the prospect's inline question text (~14%).
- `fixtures/anonymized/zillow/` — checked-in Zillow `.eml` files. All carry the canonical `New Zillow Group Rentals Contact: <address>` subject. Body is HTML-only (no `text/plain` MIME part); the parser must extract from the HTML alternative.
- `fixtures/raw/` — full `.eml` files containing prospect PII that has NOT been cleared. **Gitignored**. Each developer keeps their own copy locally for ad-hoc parser exploration. Not exercised by CI.

The `anonymized/` directory name is a vestige of the original plan to mask names/emails/phones. Sam confirmed (2026-05) the published lead data carries no actionable PII concern, so the fixtures here are committed verbatim — real names, real emails, real phone numbers — extracted from the legacy mailbox. If you ever need to redact more aggressively, run `scripts/anonymize_fixture.py` (TBD) and replace the file in place.

## Provenance

The initial set of 20 fixtures (10 StreetEasy + 10 Zillow) was extracted from `autoreply_gmail_inbox.mbox` during H4c (2026-05). Selection criteria:

- **StreetEasy:** 7 tour-request + 3 question, spanning multiple addresses. Sample taken from the most recent 500 SE leads.
- **Zillow:** 10 leads sampled evenly across the full mailbox timeline (Aug 2024 → Apr 2026) to catch any format drift over time.

The mbox is `.gitignore`d at the repo root — it's the source you extracted from, not part of CI.

## Adding new fixtures

1. Drop the raw `.eml` into `fixtures/raw/` (gitignored) or directly into the appropriate subdirectory under `fixtures/anonymized/` if Sam has cleared it for commit.
2. Use the filename convention `<variant>__<address-slug>__<source-key>.eml` so the variant intent is visible at a glance.
3. Add a corresponding test case in `tests/parsers/test_<source>.py`.
