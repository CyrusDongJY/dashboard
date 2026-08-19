BEGIN;

CREATE TABLE IF NOT EXISTS risk_event_pulse_daily (
    report_date              date        PRIMARY KEY,
    state                    text        NOT NULL,
    state_cn                 text,
    state_detail             text,
    market_confirmation      boolean     NOT NULL DEFAULT false,
    candidate_count          int         NOT NULL DEFAULT 0,
    eligible_count           int         NOT NULL DEFAULT 0,
    candidate_coverage       numeric     NOT NULL DEFAULT 0,
    scan_coverage            numeric     NOT NULL DEFAULT 0,
    scans_completed          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    risk_on_tickers          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    risk_off_tickers         jsonb       NOT NULL DEFAULT '[]'::jsonb,
    extreme_tickers          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    positive_industries      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    negative_industries      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    qqq_return_1d_pct        numeric,
    source_date              date,
    source_name              text,
    calc_version             text        NOT NULL,
    shadow_mode              boolean     NOT NULL DEFAULT true,
    computed_at              timestamptz,
    created_at               timestamptz DEFAULT now(),
    CHECK (candidate_count >= 0),
    CHECK (eligible_count >= 0 AND eligible_count <= candidate_count),
    CHECK (candidate_coverage BETWEEN 0 AND 1),
    CHECK (scan_coverage BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS risk_event_candidate_daily (
    report_date              date        NOT NULL,
    ticker                   text        NOT NULL,
    layer                    text        NOT NULL DEFAULT 'dynamic',
    scan_codes               jsonb       NOT NULL DEFAULT '[]'::jsonb,
    scanner_rank             int,
    industry                 text,
    category                 text,
    subcategory              text,
    primary_exchange         text,
    industry_group           text,
    close                    numeric,
    return_1d_pct            numeric,
    return_5d_pct            numeric,
    qqq_return_1d_pct        numeric,
    qqq_return_5d_pct        numeric,
    relative_1d_pct          numeric,
    relative_5d_pct          numeric,
    dollar_volume_proxy      numeric,
    turnover_ratio_20d       numeric,
    abnormal_turnover_z60    numeric,
    above_20d                boolean,
    above_50d                boolean,
    member_score             numeric,
    active_direction         text,
    event_suspect            boolean     NOT NULL DEFAULT false,
    pulse_direction          text        NOT NULL DEFAULT 'NONE',
    single_name_extreme      boolean     NOT NULL DEFAULT false,
    catalyst_status          text        NOT NULL DEFAULT 'UNVERIFIED',
    source_date              date,
    source_name              text,
    sample_len               int,
    quality                  text        NOT NULL,
    decision_eligible        boolean     NOT NULL DEFAULT false,
    calc_version             text        NOT NULL,
    created_at               timestamptz DEFAULT now(),
    PRIMARY KEY (report_date, ticker),
    CHECK (member_score IS NULL OR member_score BETWEEN 0 AND 100)
);

CREATE INDEX IF NOT EXISTS idx_risk_event_state_date
    ON risk_event_pulse_daily (state, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_risk_event_candidate_ticker_date
    ON risk_event_candidate_daily (ticker, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_risk_event_candidate_direction_date
    ON risk_event_candidate_daily (pulse_direction, report_date DESC);

COMMIT;
