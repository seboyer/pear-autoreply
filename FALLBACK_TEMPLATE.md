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

> Hi {{first_name|there}}! 
>
>Thank you so much for showing interest in {{apartment_address|my listing}}. The faster I can get a bit more information, the faster we can schedule a showing in person or virtually! 
>
>Please let me know if your credit score is in good standing (generally 680 is considered good, but a lot of owners will consider a lower score). 
>
>If you are applying alone, or with roommates and/or a partner, do you make a combined income of 40 x the rent? (if the unit is 2000 then usually 80 k income is required). If you have guarantors, a new job offer or decent savings then this may not apply. 
>
>Do you have a pet or pets? 
>
>When are you looking to move in?
>
>What is your availability during the week and on the weekend?
>
>If you are using a government subsidy or voucher, please provide the details. Please note: if the voucher covers the full rent, the credit score requirement does not apply.
>
>*If you already answered these questions in an original email through StreetEasy, Trulia or Zillow, please forward that. 
>
>Happy Hunting, 

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

Edit the template body section above, then **rebuild and deploy** — the edit does not reach production any other way.

This file is baked into the Docker image at build time (`COPY FALLBACK_TEMPLATE.md ./` in `Dockerfile`) and read from the project root at request time. It is *not* volume-mounted on Render, so a restart re-reads the same image bytes: an edit that isn't in the deployed image cannot take effect. `autoDeploy` is off, so trigger the deploy manually (see RENDER_MIGRATION.md).

`POST /admin/reload-template` does **not** work today — it is a 501 stub (`routes/admin.py`). The in-process cache-drop it is meant to call (`services/templates.py::reload_pear_fallback_template`) exists but is unwired; even once wired, it would only help if this file could change on disk without a rebuild, which it cannot on Render.

The repeat-inquiry fallback has the same constraints — see `FALLBACK_REPEAT_TEMPLATE.md`.
