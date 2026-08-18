BEGIN;

CREATE TABLE IF NOT EXISTS risk_capital_daily (
    report_date              date        PRIMARY KEY,
    state                    text        NOT NULL,
    state_cn                 text,
    state_detail             text,
    score                    numeric,
    coverage                 numeric     NOT NULL DEFAULT 0,
    confidence               numeric     NOT NULL DEFAULT 0,
    qqq_return_1d_pct        numeric,
    qqq_return_5d_pct        numeric,
    relative_breadth_1d      numeric,
    relative_breadth_5d      numeric,
    positive_breadth_1d      numeric,
    positive_breadth_5d      numeric,
    turnover_breadth         numeric,
    active_risk_on           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    active_deleveraging      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    event_suspects           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    layer_details            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    source_date              date,
    source_name              text,
    calc_version             text        NOT NULL,
    shadow_mode              boolean     NOT NULL DEFAULT true,
    computed_at              timestamptz,
    created_at               timestamptz DEFAULT now(),
    CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    CHECK (coverage BETWEEN 0 AND 1),
    CHECK (confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS risk_capital_member_daily (
    report_date              date        NOT NULL,
    ticker                   text        NOT NULL,
    layer                    text        NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_risk_capital_state_date
    ON risk_capital_daily (state, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_risk_capital_member_ticker_date
    ON risk_capital_member_daily (ticker, report_date DESC);

COMMIT;
