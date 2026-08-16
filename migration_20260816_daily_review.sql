BEGIN;

ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_previous_close numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_open_price numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_close_price numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_open_to_close_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_last_vwap numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_close_vs_vwap_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_previous_close numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_open_price numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_close_price numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_open_to_close_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_last_vwap numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_close_vs_vwap_pct numeric;

CREATE TABLE IF NOT EXISTS daily_review (
    report_date        date        PRIMARY KEY,
    rule_version       text        NOT NULL,
    data_quality       text        NOT NULL
                                CHECK (data_quality IN ('HIGH', 'MEDIUM', 'LOW')),
    structure_state    text,
    tactical_state     text,
    risk_state         text,
    summary            text,
    quality_details    jsonb,
    modules            jsonb,
    changes            jsonb,
    anomalies          jsonb,
    price_map          jsonb,
    scenarios          jsonb,
    exposure_context   jsonb,
    input_dates        jsonb,
    computed_at        timestamptz,
    source_date        date,
    as_of_time         timestamptz,
    ingested_at        timestamptz,
    created_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_daily_review_quality_date
    ON daily_review (data_quality, report_date DESC);

COMMIT;
