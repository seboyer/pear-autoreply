-- Add RFC-822 Message-ID columns to public.inquiries (cross-system stitching, Part A).
--
-- WHY: message-monitor stitches conversations ↔ inquiries on the RFC-822 `Message-ID`
-- header (stable + identical across all Hiver mailbox copies), NOT the per-account
-- Gmail id. See ~/Dev/autoreplies/HIVER_DEDUP_COMPARISON.md and the message-monitor
-- spec detector-c-rfc822-foundation-spec.md (Part A).
--
-- HOW TO APPLY: autoreplies has no migration runner and connects to Supabase over
-- PostgREST only (its service key is REST-only, not a DB password). Apply this once to
-- the SHARED project `fuacxndojzybijrqdbym` as the `postgres`/owner role — e.g. via the
-- Supabase SQL editor, or coordinate with a message-monitor session that holds the
-- migration DSN. Additive + idempotent (IF NOT EXISTS); safe to re-run.
--
-- ORDERING: apply this BEFORE enabling autoreplies' `WRITE_RFC822_MESSAGE_ID=true`.
-- The code ships with the flag OFF (default), so it is safe to deploy first; flipping
-- the flag before these columns exist would make the inquiry upsert fail (PostgREST
-- 400 "column not found") and drop leads.
--
-- Normalization contract: autoreplies stores the RAW `Message-ID` header value —
-- angle brackets kept, case preserved (e.g. <abc@notifications.google.com>) — to match
-- the form message-monitor stores in monitor.messages.rfc822_message_id, so the join is
-- literal-equal.

ALTER TABLE public.inquiries ADD COLUMN IF NOT EXISTS rfc822_message_id text;
ALTER TABLE public.inquiries ADD COLUMN IF NOT EXISTS reply_rfc822_message_id text;

CREATE INDEX IF NOT EXISTS inquiries_rfc822_message_id_idx
    ON public.inquiries (rfc822_message_id);

-- message-monitor's `monitor_app` role already has SELECT on public.inquiries
-- (and, post-migration 0027, RLS read access), so no new grant is needed for it to
-- read these columns.
