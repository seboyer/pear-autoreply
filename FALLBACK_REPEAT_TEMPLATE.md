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

- This template is loaded at startup from this file. After editing, the service needs a reload (or restart) to pick up changes — no deploy required if the file is volume-mounted.
- This fallback applies only to the repeat-inquiry path (Phase 2). For first-touch replies, see `FALLBACK_TEMPLATE.md`.
