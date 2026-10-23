BEGIN;

-- Independent append-only shadow snapshots. Replay cannot replace issued data.
CREATE TABLE IF NOT EXISTS public.core_funding_daily (
    report_date date NOT NULL,
    calc_version text NOT NULL,
    observation_mode text NOT NULL CHECK (observation_mode IN ('LIVE_SHADOW', 'PIT_REPLAY')),
    as_of timestamptz NOT NULL,
    score numeric CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    coverage numeric NOT NULL CHECK (coverage BETWEEN 0 AND 1),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, calc_version, observation_mode),
    CHECK ((as_of AT TIME ZONE 'America/New_York')::date = report_date),
    CHECK ((result ->> 'shadow_mode')::boolean IS TRUE),
    CHECK ((result ->> 'decision_eligible')::boolean IS FALSE),
    CHECK (score IS NULL OR coverage = 1)
);

ALTER TABLE public.core_funding_daily ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.core_funding_daily FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON public.core_funding_daily TO service_role;

CREATE INDEX IF NOT EXISTS core_funding_mode_date
    ON public.core_funding_daily (observation_mode, calc_version, report_date DESC);

NOTIFY pgrst, 'reload schema';
COMMIT;
