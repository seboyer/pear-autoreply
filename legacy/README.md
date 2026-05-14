# Legacy reference

Files in this folder are **not part of the running application**. They're kept verbatim for reference while we migrate behavior off the old Zapier-based pipeline.

- `zapier_supabase_post.py` — the original Zapier "Run Python" code that POSTs an inquiry into the Supabase `inquiries` table. Source of truth for the field naming and the `Prefer: resolution=merge-duplicates` semantics. Behavior preserved by `src/autoreplies/services/supabase.py` and `src/autoreplies/utils/coerce.py`.

When the Phase 3 Supabase writer is fully implemented and verified against this script's behavior, this folder can be deleted (or moved into a `docs/` archive).
