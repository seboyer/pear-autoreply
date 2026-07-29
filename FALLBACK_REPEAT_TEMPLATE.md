# Pear NYC — Generic Fallback Repeat-Inquiry Autoreply Template

Used when an agent's `Users.Autoreply Repeat Template (Agent)` field in Airtable is empty and a prospect's repeat inquiry is detected (same person contacting the same agent mailbox within 14 days). Each agent's own repeat template takes precedence; this is the safety net so a repeat inquiry always receives a meaningful reply.

## Slot syntax

`{{slot}}` — required, will fail safety check if no value.
`{{slot|fallback}}` — optional, the literal fallback string is used when the slot is null.

## Available slots

| Slot | Always available? | Notes |
|---|---|---|
| `first_name` | No (Zillow never; StreetEasy ~96%) | Use `{{first_name|there}}` |
| `apartment_address` | StreetEasy ~95.5%, Zillow 100% | Use `{{apartment_address|the listing}}` |

## Template body

> Hi {{first_name|there}}!
>
> Thank you for your continued interest in working with us! I got your additional inquiry for {{apartment_address|this listing}}. Please let me know if anything has changed or you have any additional questions.
>
> Happy Hunting,

## Implementation notes

- **Editing this file requires a rebuild + deploy.** It is baked into the Docker image at build time (`COPY FALLBACK_REPEAT_TEMPLATE.md ./` in `Dockerfile`) and read from the project root at request time. It is *not* volume-mounted on Render, so a restart re-reads the same image bytes — an edit that isn't in the deployed image cannot take effect. `autoDeploy` is off, so trigger the deploy manually (see RENDER_MIGRATION.md).
- There is no hot-reload path for this template. `services/templates.py::_load_repeat_fallback_template` is `@lru_cache`d with no `reload_*` counterpart (the first-touch loader has `reload_pear_fallback_template()`; this one does not), and `POST /admin/reload-template` is still a 501 stub.
- This fallback applies only to the repeat-inquiry path (Phase 2). For first-touch replies, see `FALLBACK_TEMPLATE.md`.
