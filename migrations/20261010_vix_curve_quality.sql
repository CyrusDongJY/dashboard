-- VIX standard-monthly curve provenance. Additive and safe to rerun.
BEGIN;
ALTER TABLE public.macro_spot_daily
    ADD COLUMN IF NOT EXISTS vix_curve_quality text;
ALTER TABLE public.macro_spot_daily
    ADD COLUMN IF NOT EXISTS vix_curve_metadata jsonb;
ALTER TABLE public.macro_spot_daily
    ADD COLUMN IF NOT EXISTS vix_curve_source_date date;
COMMENT ON COLUMN public.macro_spot_daily.vix_curve_quality IS
    'OK only for verified standard-monthly contracts and aligned current-day prices; NULL denotes unverified legacy observations.';
NOTIFY pgrst, 'reload schema';
COMMIT;
