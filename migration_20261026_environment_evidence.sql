-- Additive migration. No existing score, report or historical row is rewritten.
BEGIN;
ALTER TABLE public.environment_daily
    ADD COLUMN IF NOT EXISTS details jsonb NOT NULL DEFAULT '{}'::jsonb;
COMMIT;
