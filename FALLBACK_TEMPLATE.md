# Pear NYC — Generic Fallback Autoreply Template

Used when an agent's `Users.Autoreply (Agent)` rich-text field in Airtable is empty. Each agent's own template takes precedence; this is the safety net so we never miss a reply.

> **Status:** placeholder pending revision. Sam plans to coordinate with the sales team to add qualification questions (budget, move-in date, # of occupants, etc.) — revisit this draft after that conversation.

## Slot syntax

`{{slot}}` — required, will fail safety check if no value.
`{{slot|fallback}}` — optional, the literal fallback string is used when the slot is null.

## Available slots

| Slot | Always available? | Notes |
|---|---|---|
| `first_name` | No (Zillow never; StreetEasy ~96%) | Use `{{first_name|there}}` |
| `apartment_address` | StreetEasy ~95.5%, Zillow 100% | Use `{{apartment_address|the listing}}` |

The agent's name, phone, license info, etc. are **not** template slots — they live in the agent's Gmail signature, which the system appends automatically at send time.

## Template body

> Hi {{first_name|there}},
>
> Thanks for your interest in {{apartment_address|the listing}}! I'd love to help you find a great fit in NYC.
>
> Are you available for a showing this week? If you can share a few day/time options, I'll get something on the calendar. In the meantime, feel free to reply with any questions about the apartment, the neighborhood, or our application process.
>
> Talk soon,

## How it renders

The system appends the agent's default Gmail signature directly below "Talk soon," at send time. So a sent reply looks like (signatures shown for illustration only — each agent's actual signature is whatever they have configured in Gmail):

**StreetEasy lead with full data:**
> Hi Casey,
>
> Thanks for your interest in 123 Main St #4B! I'd love to help you find a great fit in NYC.
>
> Are you available for a showing this week? […]
>
> Talk soon,
>
> *— [agent's Gmail signature appended here] —*
> *Jane Doe*
> *Licensed Real Estate Salesperson*
> *Pear NYC · (646) 555-0123 · jane@pearnyc.com*

**Zillow lead (no name, no phone — just email + address from the platform):**
> Hi there,
>
> Thanks for your interest in 456 Oak Ave! I'd love to help you find a great fit in NYC.
>
> Are you available for a showing this week? […]
>
> Talk soon,
>
> *— [agent's Gmail signature appended here] —*
> *Jane Doe*
> *Licensed Real Estate Salesperson*
> *Pear NYC · (646) 555-0123 · jane@pearnyc.com*

## Implementation notes

- Reply is sent as `multipart/alternative`. Plain-text part contains the template body only (signature is HTML, omitted from plain). HTML part contains the formatted template body + the agent's signature HTML appended after the closing line.
- Signatures are fetched via `users.settings.sendAs.get` (the entry where `isDefault = true`, falling back to the entry matching the agent's primary mailbox). Cached in Redis with a 24h TTL.
- If signature fetch fails or the agent has no default signature configured, the reply still sends — just without a signature — and a Slack warning is posted so the agent can fix it.

## Editing

Edit the template body section above. The system loads it from this file at startup. After editing, ping the service to reload via `POST /admin/reload-template` (bearer token required) — no redeploy needed.
