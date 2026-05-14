# TODO

Items deferred for later — not blocking the current phase of work but worth coming back to.

## Operational caveats

- **First run of `harness diff`**: the prod-side fetch is scoped to `Gmail Message ID (Autoreply) != ""`, which loads every autoreply-tagged Inquiry since that field was added in prod. On the first invocation, use a `--since` very close to today so the result set stays small. Widen the window only after confirming the diff output shape is sane.
